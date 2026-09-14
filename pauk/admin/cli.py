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

from pauk.admin import feed
from pauk.admin.auth import ROLES, AuthError, create_user, list_users, set_active
from pauk.graph import prune
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
    CREATE,
    DELETE,
    LINK,
    active_overrides,
    apply_overrides,
    deactivate_override,
    deactivate_relationship_override,
    record_override,
    record_relationship_override,
)
from pauk.jobs.locks import Busy
from pauk.jobs.worker import POLL_SECONDS as WORKER_POLL
from pauk.jobs.worker import Worker
from pauk.settings import Settings
from pauk.storage.prepared import REVISIONS, trim_revisions

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
    create.add_argument("--note", default=None, help="why the record was added")
    create.add_argument("--once", action="store_true",
                        help="create without claiming it; a prune will then treat the "
                             "record as a leftover")

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
            rel.add_argument("--note", default=None, help="why the link was added")
            rel.add_argument("--once", action="store_true",
                             help="link without claiming it; a prune will then treat the "
                                  "link as a leftover")
        else:
            rel.add_argument("--note", default=None, help="why the link was removed")
            rel.add_argument("--once", action="store_true",
                             help="unlink without recording it; the next publish restores the link")

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
    undo_rel = overrides.add_parser(
        "undo-rel", help="restore a link removed by hand; the next publish rebuilds it")
    for argument in ("src_label", "rel_type", "tgt_label", "src_id", "tgt_id"):
        undo_rel.add_argument(argument)

    commands.add_parser("schema", help="list the labels, fields and relationships that can be edited")

    graph_prune = commands.add_parser(
        "prune", help="remove from the graph what the prepared rows no longer ask for")
    graph_prune.add_argument(
        "--apply", action="store_true",
        help="remove them; without it the list is printed and nothing changes")
    graph_prune.add_argument(
        "--limit", type=int, default=20,
        help="how many of each kind to print (default: 20)")

    trim = commands.add_parser(
        "trim", help="shorten the two histories that only ever grow: the change "
                     "feed and the archive of replaced rows")
    trim.add_argument(
        "--keep-days", type=int, default=feed.KEEP_DAYS,
        help=f"how much history to keep (default: {feed.KEEP_DAYS})")
    trim.add_argument(
        "--apply", action="store_true",
        help="delete them; without it the counts are printed and nothing changes")

    worker = commands.add_parser(
        "worker", help="perform the scheduled runs, one at a time")
    worker.add_argument("--once", action="store_true",
                        help="take at most one job and exit, instead of waiting for more")
    worker.add_argument("--poll", type=float, default=WORKER_POLL,
                        help=f"seconds to wait on an empty queue (default: {WORKER_POLL:g})")
    worker.add_argument("--name", default=None,
                        help="how this worker is recorded on the jobs it takes "
                             "(default: host:pid)")

    user = commands.add_parser("user", help="panel accounts").add_subparsers(
        dest="user_command", required=True)
    user_add = user.add_parser("add", help="create an account")
    user_add.add_argument("login")
    user_add.add_argument("--role", choices=ROLES, default="editor")
    user.add_parser("list", help="show the accounts")
    for name, help_text in (("disable", "block an account and end its sessions"),
                            ("enable", "let a blocked account back in")):
        toggle = user.add_parser(name, help=help_text)
        toggle.add_argument("login")


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

    # Accounts live in Mongo alone: no graph connection, and nothing to
    # audit into the change feed of the graph.
    if args.admin_command == "user":
        _run_user(args, db)
        return

    # Same: both histories are Mongo collections, and shortening them is not
    # a change to the graph that anything would record.
    if args.admin_command == "trim":
        _run_trim(args, db)
        return

    # The worker opens its own connections, per job and per step, because a
    # single one held open for hours is a connection that dies quietly. It
    # also sets its own actor: each job records who asked for it.
    if args.admin_command == "worker":
        _run_worker(args, config, db)
        return

    actor = args.actor or f"user:{getpass.getuser()}"
    client = audited_client(config, db)
    try:
        with actor_context(actor, source="admin-cli"):
            if args.admin_command == "prune":
                _run_prune(args, client, db)
            else:
                _dispatch(args, client, db, actor)
    except MutationError as error:
        raise SystemExit(str(error)) from None
    finally:
        client.close()


def _run_worker(args, config: Settings, db: Database) -> None:
    """Perform scheduled runs until asked to stop.

    A second process next to the panel, not a thread inside it: a
    collection run takes hours, and restarting the web service must not cut
    one in half.
    """
    worker = Worker(config=config, db=db, name=args.name or "",
                    poll_seconds=args.poll)
    if args.once:
        if not worker.run_once():
            print("nothing queued")
        return
    worker.run_forever()


def _run_prune(args, client, db: Database) -> None:
    """Bring the graph back to what the prepared rows describe.

    Listing by default. The graph is what the map and the panel read, and
    a deletion nobody looked at first is the wrong way round for a step
    that exists because the two copies had drifted apart unnoticed.

    The comparison and the removal are one turn under the graph lock, so
    this cannot run while a publish is writing — see `prune.run`.
    """
    try:
        plan = prune.run(client, db, args.apply)
    except Busy as error:
        raise SystemExit(str(error)) from None
    for label, ids in sorted(plan.nodes.items()):
        print(f"{label}: {len(ids)} record(s) no row explains")
        for node_id in ids[:args.limit]:
            print(f"    {node_id}")
        if len(ids) > args.limit:
            print(f"    ... and {len(ids) - args.limit} more")
    for (src_label, rel_type, tgt_label), pairs in sorted(plan.edges.items()):
        print(f"({src_label})-[:{rel_type}]->({tgt_label}): {len(pairs)} link(s) no row asks for")
        for src_id, tgt_id in pairs[:args.limit]:
            print(f"    {src_id} -> {tgt_id}")
        if len(pairs) > args.limit:
            print(f"    ... and {len(pairs) - args.limit} more")
    if plan.kept_by_hand:
        print(f"kept: {plan.kept_by_hand} added by hand and claimed as a decision")
    if plan.folding:
        print(f"kept: {plan.folding} id(s) the next publish folds into another record")
    if not plan.total():
        print("the graph matches the prepared rows")
        return
    if not args.apply:
        print(f"\n{plan.total()} in all; nothing removed, pass --apply to remove them")
        return
    print(f"\nremoved {plan.removed['pruned_relationships']} link(s) "
          f"and {plan.removed['pruned_nodes']} record(s)")


def _run_trim(args, db: Database) -> None:
    """Keep the two growing histories from growing for ever.

    Both at once because they grow for the same reason and are shortened on
    the same schedule: the change feed records what happened to the graph,
    the revision archive what a prepared row said before a run replaced it.

    Counting by default. A cut of either is not something to discover
    afterwards, so the size of it is printed first and made only when asked
    for.
    """
    cutoff = feed.older_than(args.keep_days)
    entries = feed.trim(db, cutoff, apply=args.apply)
    versions = trim_revisions(db, cutoff, apply=args.apply)
    if not args.apply:
        print(f"older than {args.keep_days} days (before {cutoff[:10]}):")
        print(f"    change feed:      {entries['audit_matched']} of {feed.count(db)}")
        print(f"    replaced rows:    {versions['revisions_matched']} of "
              f"{db[REVISIONS].count_documents({})}")
        print("nothing removed; pass --apply to remove them")
        return
    print(f"removed {entries['audit_removed']} feed entr(y/ies) "
          f"and {versions['revisions_removed']} archived version(s)")


def _run_user(args, db: Database) -> None:
    """Manage the accounts that can log into the panel.

    The password is read from a prompt, never from an argument: anything
    passed on the command line lands in the shell history and in `ps`.
    """
    if args.user_command == "add":
        password = getpass.getpass(f"password for {args.login}: ")
        if password != getpass.getpass("repeat: "):
            raise SystemExit("passwords do not match")
        try:
            created = create_user(db, args.login, password, role=args.role)
        except AuthError as error:
            raise SystemExit(str(error)) from None
        # Print the stored login, not the typed one: logins are lowercased
        # on the way in.
        print(f"created {created['_id']} ({args.role})")
    elif args.user_command == "list":
        rows = list_users(db)
        if not rows:
            print("no accounts yet; add one with `pauk admin user add <login>`")
        for row in rows:
            state = "active" if row.get("active") else "blocked"
            print(f"  {row['_id']:<20} {row.get('role', '?'):<8} {state}")
    else:
        wanted = args.user_command == "enable"
        if not set_active(db, args.login, wanted):
            raise SystemExit(f"no such user: {args.login}")
        print(f"{args.login} is now {'active' if wanted else 'blocked'}")


def _dispatch(args, client, db, actor: str) -> None:
    if args.admin_command == "node":
        _run_node(args, client, db, actor)
    elif args.admin_command == "rel":
        _run_relationship(args, client, db, actor)
    elif args.admin_command == "overrides":
        _run_overrides(args, client, db)
    else:
        _run_merge(args, client)


def _run_node(args, client, db, actor: str) -> None:
    if args.node_command == "show":
        print(json.dumps(read_node(client, args.label, args.id),
                         ensure_ascii=False, indent=2, default=str))
    elif args.node_command == "create":
        fields = _parse_assignments(args.assignments)
        node = create_node(client, args.label, args.id, fields)
        # Claimed, not reapplied: no prepared row will ever explain this
        # record, and a prune would take it for a leftover of one.
        if db is not None and not args.once:
            record_override(db, args.label, args.id, CREATE, fields, actor=actor,
                            note=args.note or "")
        logger.info("created %s %s%s", args.label, args.id,
                    "" if db is not None and not args.once else " (not recorded as a decision)")
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
    says. So the edit is also kept as an override, with the automatic value
    it replaces, so the conflict screen can later say what the source now
    claims. The graph is written first and the decision recorded second —
    see the comment below for why that order matters.
    """
    fields = _parse_assignments(args.assignments)
    before = read_node(client, args.label, args.id)
    # The graph write goes first. It is the step that can be refused — by a
    # version conflict, or by validation — and a decision recorded for an
    # edit that never happened would be applied by the next publish, quietly
    # making a change the person was just told was rejected.
    node = update_node(client, args.label, args.id, fields,
                       expected_updated_at=args.expect_updated_at)
    if db is not None and not args.once:
        record_override(db, args.label, args.id, "set", fields, actor=actor,
                        note=args.note or "",
                        auto_value={name: before.get(name) for name in fields})
    logger.info("updated %s %s%s", args.label, args.id,
                "" if db is not None and not args.once else " (not recorded as an override)")
    return node


def _delete(args, client, db, actor: str) -> None:
    """Remove a node, and tombstone it so publishing does not bring it back."""
    # Same order as _set_fields: a node with relationships and no --cascade
    # is refused, and a tombstone left behind would delete it on the next
    # publish anyway.
    # Snapshot first, like the panel does: afterwards the node is gone, and
    # the decision has to carry what it removed or the record can only be
    # restored from the feed — which keeps history, not state.
    snapshot = read_node(client, args.label, args.id)
    removed = delete_node(client, args.label, args.id, cascade=args.cascade)
    if db is not None and not args.once:
        record_override(db, args.label, args.id, "delete", actor=actor, note=args.note or "",
                        snapshot={name: value for name, value in snapshot.items()
                                  if name in NODE_FIELDS[args.label] and value is not None})
    logger.info("deleted %d node(s)", removed)


def _run_overrides(args, client, db) -> None:
    if db is None:
        raise SystemExit("overrides live in MongoDB; none is configured")
    if args.overrides_command == "list":
        rows = active_overrides(db)
        if not rows:
            print("no active overrides")
        for row in rows:
            if row.get("kind") == "rel":
                what = (f"unlink ({row['src_label']} {row['src_id']})-[:{row['rel_type']}]->"
                        f"({row['tgt_label']} {row['target_id']})")
            else:
                fields = ", ".join(f"{k}={v!r}" for k, v in (row.get("fields") or {}).items())
                what = f"{row['op']}  {fields}"
            print(f"{row['_id']}  {what}  by {row['actor']}"
                  f"{'  — ' + row['note'] if row.get('note') else ''}")
    elif args.overrides_command == "undo":
        if deactivate_override(db, args.label, args.id):
            print(f"override for {args.label} {args.id} switched off; "
                  f"run `pauk admin overrides apply` or republish to restore the automatic value")
        else:
            raise SystemExit(f"no override recorded for {args.label} {args.id}")
    elif args.overrides_command == "undo-rel":
        # Only the removal: a link somebody added shares the document id
        # with one somebody removed, and this command is about the second.
        if deactivate_relationship_override(db, args.src_label, args.rel_type, args.tgt_label,
                                            args.src_id, args.tgt_id, only_op=DELETE):
            print(f"({args.src_label} {args.src_id})-[:{args.rel_type}]->"
                  f"({args.tgt_label} {args.tgt_id}) will be rebuilt by the next publish")
        else:
            raise SystemExit("no override recorded for that relationship")
    else:
        result = apply_overrides(client, db)
        print(", ".join(f"{key}={value}" for key, value in result.items()))


def _run_relationship(args, client, db, actor: str) -> None:
    if args.rel_command == "add":
        # Nothing reapplies this: the loader only ever creates edges, so one
        # added by hand is never taken away by a publish. It is claimed so a
        # prune can tell it from an edge the pipeline has stopped making.
        create_relationship(client, args.src_label, args.rel_type, args.tgt_label,
                            args.src_id, args.tgt_id, _parse_assignments(args.assignments))
        if db is not None and not args.once:
            record_relationship_override(db, args.src_label, args.rel_type, args.tgt_label,
                                         args.src_id, args.tgt_id, op=LINK, actor=actor,
                                         note=args.note or "")
        logger.info("linked (%s %s)-[:%s]->(%s %s)%s", args.src_label, args.src_id,
                    args.rel_type, args.tgt_label, args.tgt_id,
                    "" if db is not None and not args.once else " (not recorded as a decision)")
        return

    # Deletion is the direction that needs remembering: the same prepared
    # row rebuilds the edge on the next publish.
    removed = delete_relationship(client, args.src_label, args.rel_type, args.tgt_label,
                                  args.src_id, args.tgt_id)
    if db is not None and not args.once:
        record_relationship_override(db, args.src_label, args.rel_type, args.tgt_label,
                                     args.src_id, args.tgt_id, actor=actor, note=args.note or "")
    logger.info("removed %d relationship(s)%s", removed,
                "" if db is not None and not args.once else " (not recorded as an override)")


def _run_merge(args, client) -> None:
    # Merging deletes the duplicate together with its relationships, and
    # the audit diff covers node properties only. A Person can still be
    # rebuilt from its prepared row (pauk.graph.unmerge), which is what the
    # review queue's "split back" does; nothing rebuilds the other labels.
    if not args.yes:
        answer = input(
            f"Merge {args.label} {args.duplicate_id} into {args.canonical_id}?\n"
            "The duplicate and its relationships are removed. Only a Person can be "
            "put back, from the panel, and only while its prepared row is there. [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            raise SystemExit("cancelled")
    removed = merge_nodes(client, args.label, args.duplicate_id, args.canonical_id)
    logger.info("merged %d node(s) into %s", removed, args.canonical_id)
