"""`pauk admin ...` — editing the graph from the shell.

The first consumer of `pauk.graph.mutations`, and a complete one: every
operation the panel will offer is reachable here. Whatever the panel adds
on top is a form and a login, not different rules.

This module does three things and no more — parse arguments, name the
actor, print the result. Validation and the writes themselves belong to
the mutation layer.
"""

from __future__ import annotations

import getpass
import json
import logging

from pymongo.database import Database

from pauk.graph.audit import actor_context, audited_client
from pauk.graph.mutations import (
    NODE_FIELDS,
    RELATIONSHIPS,
    MutationError,
    create_node,
    create_relationship,
    delete_node,
    delete_relationship,
    merge_nodes,
    read_node,
    update_node,
)
from pauk.graph.overrides import (
    active_overrides,
    apply_overrides,
    deactivate_override,
    record_override,
)
from pauk.settings import Settings

logger = logging.getLogger("pauk.admin")


def _parse_value(raw: str):
    """`--set stars_num=10` should store a number, not the text "10".

    JSON covers numbers, booleans, null and lists in one rule; anything it
    rejects is taken as a plain string, which is what a name or a URL is.
    """
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _parse_assignments(pairs: list[str] | None) -> dict:
    fields = {}
    for pair in pairs or []:
        name, separator, value = pair.partition("=")
        if not separator:
            raise SystemExit(f"--set expects field=value, got {pair!r}")
        fields[name.strip()] = _parse_value(value)
    return fields


def add_parser(subparsers) -> None:
    """Register `pauk admin` and its subcommands."""
    parser = subparsers.add_parser("admin", help="edit the graph by hand")
    parser.add_argument("--actor", default=None,
                        help="who is making the change; recorded in the audit log "
                             "(default: the OS user)")
    commands = parser.add_subparsers(dest="admin_command", required=True)

    node = commands.add_parser("node", help="nodes").add_subparsers(
        dest="node_command", required=True)

    show = node.add_parser("show", help="print a node's properties")
    show.add_argument("label", choices=sorted(NODE_FIELDS))
    show.add_argument("id")

    create = node.add_parser("create", help="add a node the pipeline does not know")
    create.add_argument("label", choices=sorted(NODE_FIELDS))
    create.add_argument("id")
    create.add_argument("--set", dest="assignments", action="append", metavar="FIELD=VALUE")

    node_set = node.add_parser("set", help="change fields on a node")
    node_set.add_argument("label", choices=sorted(NODE_FIELDS))
    node_set.add_argument("id")
    node_set.add_argument("--set", dest="assignments", action="append", metavar="FIELD=VALUE",
                          required=True)
    node_set.add_argument("--expect-updated-at", default=None,
                          help="refuse the write if the node changed since this timestamp")
    node_set.add_argument("--note", default=None, help="why the change was made")
    node_set.add_argument("--once", action="store_true",
                          help="write to the graph without recording an override; the next "
                               "publish will overwrite it")

    node_delete = node.add_parser("delete", help="remove a node")
    node_delete.add_argument("label", choices=sorted(NODE_FIELDS))
    node_delete.add_argument("id")
    node_delete.add_argument("--cascade", action="store_true",
                             help="delete its relationships too; without this a connected "
                                  "node is left alone")
    node_delete.add_argument("--note", default=None, help="why the node was removed")
    node_delete.add_argument("--once", action="store_true",
                             help="delete without a tombstone; the next publish recreates it")

    relationship = commands.add_parser("rel", help="relationships").add_subparsers(
        dest="rel_command", required=True)
    for name, help_text in (("add", "connect two nodes"), ("delete", "disconnect two nodes")):
        rel = relationship.add_parser(name, help=help_text)
        rel.add_argument("src_label")
        rel.add_argument("rel_type")
        rel.add_argument("tgt_label")
        rel.add_argument("src_id")
        rel.add_argument("tgt_id")
        if name == "add":
            rel.add_argument("--set", dest="assignments", action="append", metavar="FIELD=VALUE")

    merge = commands.add_parser(
        "merge", help="fold a duplicate node into the node it duplicates (irreversible)")
    merge.add_argument("label", choices=sorted({"Person", "Publication", "Repository"}))
    merge.add_argument("duplicate_id")
    merge.add_argument("canonical_id")
    merge.add_argument("--yes", action="store_true",
                       help="skip the confirmation prompt")

    overrides = commands.add_parser(
        "overrides", help="manual decisions kept so a publish cannot undo them").add_subparsers(
        dest="overrides_command", required=True)
    overrides.add_parser("list", help="show the manual edits in force")
    overrides.add_parser("apply", help="reapply them to the graph")
    undo = overrides.add_parser("undo", help="stop applying one, keeping the record of it")
    undo.add_argument("label", choices=sorted(NODE_FIELDS))
    undo.add_argument("id")

    commands.add_parser("schema", help="list the labels, fields and relationships that can be edited")


def _print_schema() -> None:
    print("Labels and editable fields:")
    for label in sorted(NODE_FIELDS):
        print(f"  {label}")
        print(f"    {', '.join(sorted(NODE_FIELDS[label]))}")
    print("\nRelationships:")
    for source, rel_type, target in sorted(RELATIONSHIPS):
        print(f"  ({source})-[:{rel_type}]->({target})  target matched by "
              f"{RELATIONSHIPS[(source, rel_type, target)]}")


def run(args, config: Settings, db: Database | None) -> None:
    """Execute one `pauk admin` command.

    Args:
        args: Parsed arguments.
        config: Settings, for the Neo4j connection and the audit path.
        db: Mongo database the audit feed is written to. None for `schema`,
            which touches no database at all.
    """
    if args.admin_command == "schema":
        _print_schema()
        return

    actor = args.actor or f"user:{getpass.getuser()}"
    client = audited_client(config, db)
    try:
        with actor_context(actor, source="admin-cli"):
            _dispatch(args, client, db, actor)
    except MutationError as error:
        raise SystemExit(str(error)) from None
    finally:
        client.close()


def _dispatch(args, client, db, actor: str) -> None:
    if args.admin_command == "node":
        _run_node(args, client, db, actor)
    elif args.admin_command == "rel":
        _run_relationship(args, client)
    elif args.admin_command == "overrides":
        _run_overrides(args, client, db)
    else:
        _run_merge(args, client)


def _run_node(args, client, db, actor: str) -> None:
    if args.node_command == "show":
        print(json.dumps(read_node(client, args.label, args.id),
                         ensure_ascii=False, indent=2, default=str))
    elif args.node_command == "create":
        node = create_node(client, args.label, args.id, _parse_assignments(args.assignments))
        logger.info("created %s %s", args.label, args.id)
        print(json.dumps(node, ensure_ascii=False, indent=2, default=str))
    elif args.node_command == "set":
        node = _set_fields(args, client, db, actor)
        print(json.dumps(node, ensure_ascii=False, indent=2, default=str))
    else:
        _delete(args, client, db, actor)


def _set_fields(args, client, db, actor: str) -> dict:
    """Change fields, and remember the decision so a publish cannot undo it.

    Writing straight to the graph would hold until the next
    `pauk publish graph` and then be overwritten by whatever the source
    says. The edit is recorded as an override first — with the automatic
    value it replaces, so the conflict screen can later say what the
    source now claims — and applied second.
    """
    fields = _parse_assignments(args.assignments)
    before = read_node(client, args.label, args.id)
    if db is not None and not args.once:
        record_override(db, args.label, args.id, "set", fields, actor=actor,
                        note=args.note or "",
                        auto_value={name: before.get(name) for name in fields})
    node = update_node(client, args.label, args.id, fields,
                       expected_updated_at=args.expect_updated_at)
    logger.info("updated %s %s%s", args.label, args.id,
                "" if db is not None and not args.once else " (not recorded as an override)")
    return node


def _delete(args, client, db, actor: str) -> None:
    """Remove a node, and tombstone it so publishing does not bring it back."""
    if db is not None and not args.once:
        record_override(db, args.label, args.id, "delete", actor=actor, note=args.note or "")
    removed = delete_node(client, args.label, args.id, cascade=args.cascade)
    logger.info("deleted %d node(s)", removed)


def _run_overrides(args, client, db) -> None:
    if db is None:
        raise SystemExit("overrides live in MongoDB; none is configured")
    if args.overrides_command == "list":
        rows = active_overrides(db)
        if not rows:
            print("no active overrides")
        for row in rows:
            fields = ", ".join(f"{k}={v!r}" for k, v in (row.get("fields") or {}).items())
            print(f"{row['_id']}  {row['op']}  {fields}  by {row['actor']}"
                  f"{'  — ' + row['note'] if row.get('note') else ''}")
    elif args.overrides_command == "undo":
        if deactivate_override(db, args.label, args.id):
            print(f"override for {args.label} {args.id} switched off; "
                  f"run `pauk admin overrides apply` or republish to restore the automatic value")
        else:
            raise SystemExit(f"no override recorded for {args.label} {args.id}")
    else:
        result = apply_overrides(client, db)
        print(", ".join(f"{key}={value}" for key, value in result.items()))


def _run_relationship(args, client) -> None:
    if args.rel_command == "add":
        create_relationship(client, args.src_label, args.rel_type, args.tgt_label,
                            args.src_id, args.tgt_id, _parse_assignments(args.assignments))
        logger.info("linked (%s %s)-[:%s]->(%s %s)", args.src_label, args.src_id,
                    args.rel_type, args.tgt_label, args.tgt_id)
    else:
        removed = delete_relationship(client, args.src_label, args.rel_type, args.tgt_label,
                                      args.src_id, args.tgt_id)
        logger.info("removed %d relationship(s)", removed)


def _run_merge(args, client) -> None:
    # Merging deletes the duplicate together with its relationships, and
    # the audit diff covers node properties only — there is nothing to
    # restore the edges from afterwards.
    if not args.yes:
        answer = input(
            f"Merge {args.label} {args.duplicate_id} into {args.canonical_id}?\n"
            "This cannot be undone: the duplicate and its relationships are removed. [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            raise SystemExit("cancelled")
    removed = merge_nodes(client, args.label, args.duplicate_id, args.canonical_id)
    logger.info("merged %d node(s) into %s", removed, args.canonical_id)
