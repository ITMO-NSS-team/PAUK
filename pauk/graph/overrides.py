"""Manual edits that survive the pipeline.

Neo4j is a display copy, not the source of truth: `pauk publish graph`
pours prepared documents into it with `MERGE ... ON MATCH SET n +=
row.properties`. A field corrected by hand and also written by the
pipeline is overwritten on the next publish; a node deleted by hand comes
back, because MERGE creates it again.

So a manual edit is kept as a *decision*, not only as a value in the
graph: one document per target in `graph_overrides`, reapplied after
every publish and after every graph-wide dedup. The graph ends up
carrying the same edit again, which is why applying has to be idempotent
— reapplying an override that is already in place must produce no audit
entry at all, or the change feed fills with edits nobody made.

Two things this buys beyond survival:

- the manual edits are a readable list, not values dissolved in the graph;
- `auto_value` (what the field held before the edit) makes conflicts
  visible: "the source now says X, your override still says Y".

Deletion is a tombstone as much as an operation. The loader has to skip
tombstoned ids *before* writing, otherwise every run recreates the node
and every reapply deletes it again — the graph would be correct and the
audit log would be nonsense.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from pymongo.database import Database

from .mutations import (
    MutationError,
    UnknownEntity,
    delete_node,
    delete_relationship,
    read_node,
    update_node,
    validate_fields,
    validate_label,
    validate_relationship,
)

logger = logging.getLogger(__name__)

COLLECTION = "graph_overrides"

SET = "set"
DELETE = "delete"
OPERATIONS = (SET, DELETE)


def _now() -> datetime:
    """Current time at the precision BSON keeps.

    Mongo stores milliseconds; a plain datetime.now() carries microseconds
    and comes back rounded, so a document returned to the caller would
    differ from the one in the database.
    """
    moment = datetime.now(UTC)
    return moment.replace(microsecond=moment.microsecond // 1000 * 1000)


def override_id(label: str, target_id: str) -> str:
    """Deterministic key, so editing the same node twice updates one document."""
    return f"node:{label}:{target_id}"


def relationship_override_id(src_label: str, rel_type: str, tgt_label: str,
                             src_id: str, tgt_id: str) -> str:
    """Key for a decision about one relationship.

    A relationship needs five parts to be named: a node is one id, an edge
    is a type plus both ends. The target id is whatever the loader matches
    the target by — `url` for a Repository, `login` for a GitHubProfile —
    so the key matches what the publish path actually compares.
    """
    return f"rel:{src_label}:{rel_type}:{tgt_label}:{src_id}:{tgt_id}"


def record_override(db: Database, label: str, target_id: str, op: str,
                    fields: dict | None = None, actor: str = "unknown",
                    note: str = "", auto_value: dict | None = None,
                    snapshot: dict | None = None) -> dict:
    """Write down a manual decision about one node.

    Fields are merged into whatever the document already holds rather than
    replacing it: two people editing different fields of the same person
    on different days must both survive, and a replace would silently drop
    the earlier edit.

    Args:
        db: Mongo database.
        label: Node label, checked against the whitelist.
        target_id: Node id the decision is about.
        op: "set" or "delete".
        fields: Field values for "set"; ignored for "delete".
        actor: Who decided, for the audit trail.
        note: Free-text reason, shown in the panel.
        auto_value: What the pipeline had before the edit. Recorded only
            for fields seen for the first time — otherwise a second edit
            would overwrite the original automatic value with the previous
            manual one, and the conflict report would compare an edit with
            an edit.
        snapshot: For a deletion, every field the node carried. The
            decision then holds what is needed to put the record back, and
            restoring stops depending on the audit feed — which records
            history, not state, and summarises a bulk operation without
            listing fields at all.

    Returns:
        The stored document.
    """
    if op not in OPERATIONS:
        raise UnknownEntity(f"unknown override operation: {op!r} (known: {', '.join(OPERATIONS)})")
    validate_label(label)
    fields = dict(fields or {})
    if op == SET:
        validate_fields(label, fields)
        if not fields:
            raise MutationError("a 'set' override with no fields changes nothing")

    now = _now()
    document_id = override_id(label, target_id)

    # Read-then-replace would lose an edit: two administrators changing
    # different fields of the same node at the same time each write back a
    # whole document built from the state they read, and the later write
    # drops the earlier one. Setting the field paths instead lets the
    # server merge them, and `created_at` is only written when the document
    # is first inserted.
    update: dict = {
        "$set": {
            "kind": "node",
            "label": label,
            "target_id": target_id,
            "op": op,
            "actor": actor,
            "active": True,
            "updated_at": now,
            **{f"fields.{name}": value for name, value in fields.items()},
        },
        "$setOnInsert": {"created_at": now},
    }
    if snapshot:
        update["$set"]["snapshot"] = snapshot
    if note:
        update["$set"]["note"] = note
    else:
        update["$setOnInsert"]["note"] = ""
    db[COLLECTION].update_one({"_id": document_id}, update, upsert=True)

    # The automatic value is recorded once per field — the first edit is the
    # one that replaced what the pipeline produced. A conditional update per
    # field keeps that true without reading the document first.
    for name, value in (auto_value or {}).items():
        db[COLLECTION].update_one(
            {"_id": document_id, f"auto_value.{name}": {"$exists": False}},
            {"$set": {f"auto_value.{name}": value}})

    logger.info("override recorded: %s %s (%s)", op, document_id, ", ".join(sorted(fields)))
    # Read back rather than return what was sent: the driver hands
    # timestamps back without a timezone, so the two would differ in type
    # depending on whether this was the first edit or a later one.
    return db[COLLECTION].find_one({"_id": document_id})


def record_relationship_override(db: Database, src_label: str, rel_type: str, tgt_label: str,
                                 src_id: str, tgt_id: str, op: str = DELETE,
                                 actor: str = "unknown", note: str = "") -> dict:
    """Write down a manual decision about one relationship.

    Only `delete` carries weight here. A relationship added by hand already
    survives publishing — the loader creates edges, it never removes the
    ones it does not know about — while a deleted one is recreated by
    `MERGE` from the same prepared row, which is what this prevents.

    Raises:
        UnknownEntity: The triple is not a relationship the graph has.
    """
    if op != DELETE:
        raise UnknownEntity(
            f"relationship overrides support only {DELETE!r}, got {op!r}; "
            "a relationship added by hand survives publishing on its own")
    validate_relationship(src_label, rel_type, tgt_label)
    now = _now()
    document_id = relationship_override_id(src_label, rel_type, tgt_label, src_id, tgt_id)
    existing = db[COLLECTION].find_one({"_id": document_id}) or {}
    db[COLLECTION].replace_one({"_id": document_id}, {
        "_id": document_id,
        "kind": "rel",
        "src_label": src_label,
        "rel_type": rel_type,
        "tgt_label": tgt_label,
        "src_id": src_id,
        "target_id": tgt_id,
        "op": DELETE,
        "actor": actor,
        "note": note or existing.get("note", ""),
        "active": True,
        "created_at": existing.get("created_at", now),
        "updated_at": now,
    }, upsert=True)
    logger.info("override recorded: unlink (%s %s)-[:%s]->(%s %s)",
                src_label, src_id, rel_type, tgt_label, tgt_id)
    return db[COLLECTION].find_one({"_id": document_id})


def deactivate_relationship_override(db: Database, src_label: str, rel_type: str, tgt_label: str,
                                     src_id: str, tgt_id: str) -> bool:
    """Stop keeping a relationship unlinked; the next publish restores it."""
    result = db[COLLECTION].update_one(
        {"_id": relationship_override_id(src_label, rel_type, tgt_label, src_id, tgt_id)},
        {"$set": {"active": False, "updated_at": _now()}})
    return result.modified_count > 0


def tombstoned_relationships(db: Database) -> set[tuple[str, str, str, str, str]]:
    """Edges the loader must not recreate, as (src_label, rel_type, tgt_label, src_id, tgt_id).

    Filtered out before the upload rather than deleted after it, for the
    same reason node tombstones are: `MERGE` would recreate the edge and
    the reapply would remove it again, writing a creation and a deletion
    into the audit log on every single run.
    """
    return {
        (row["src_label"], row["rel_type"], row["tgt_label"], row["src_id"], row["target_id"])
        for row in db[COLLECTION].find({"active": True, "kind": "rel", "op": DELETE})
    }


def deactivate_override(db: Database, label: str, target_id: str) -> bool:
    """Stop applying an override without losing the record of it.

    Undoing an edit is switching this off and reapplying, not deleting the
    document: the panel still has to show that the edit existed and who
    made it.
    """
    result = db[COLLECTION].update_one(
        {"_id": override_id(label, target_id)}, {"$set": {"active": False, "updated_at": _now()}})
    return result.modified_count > 0


def active_overrides(db: Database) -> list[dict]:
    return list(db[COLLECTION].find({"active": True}))


def tombstoned_ids(db: Database, label: str) -> set[str]:
    """Ids the loader must not write at all.

    Without this the node is recreated by every publish and removed by
    every reapply: the end state is right, but each run writes a creation
    and a deletion nobody asked for into the audit log.
    """
    return {
        row["target_id"]
        for row in db[COLLECTION].find(
            {"active": True, "kind": "node", "op": DELETE, "label": label},
            {"target_id": True})
    }


def _needs_write(current: dict, fields: dict[str, Any]) -> bool:
    """Whether the graph already agrees with the override.

    The comparison is what keeps reapplying free: without it every publish
    would write every override again and stamp an audit entry for a change
    that did not happen.
    """
    return any(current.get(name) != value for name, value in fields.items())


def apply_overrides(client, db: Database) -> dict[str, int]:
    """Reapply every active manual decision to the graph.

    Called right after an edit is saved (so it takes effect at once), at
    the end of a publish and at the end of a graph-wide dedup — both of
    which can undo manual work — and by `pauk overrides apply`.

    Args:
        client: Graph client; pass the audited one so real changes are
            recorded.
        db: Mongo database holding the overrides.

    Returns:
        Counts: how many overrides were applied, how many were already in
        place, and how many targets no longer exist.
    """
    applied = unchanged = missing = 0
    for override in active_overrides(db):
        if override.get("kind") == "rel":
            # Belt and braces: the loader already skips these, but an edge
            # created by anything else — a rerun of an older version, a hand
            # written query — is removed here.
            #
            # A row naming something the registry no longer has (a
            # relationship type dropped in a refactor, a hand-edited
            # document) is reported and skipped: this runs inside publish,
            # and one bad row must not take a whole group's publish down.
            try:
                removed = delete_relationship(
                    client, override["src_label"], override["rel_type"], override["tgt_label"],
                    override["src_id"], override["target_id"])
            except MutationError as error:
                logger.warning("override %s skipped: %s", override["_id"], error)
                missing += 1
                continue
            applied += bool(removed)
            unchanged += not removed
            continue
        label, target_id = override["label"], override["target_id"]
        try:
            current = read_node(client, label, target_id)
        except MutationError:
            # A deletion override whose node is already gone is the normal
            # steady state, not a problem worth reporting.
            if override["op"] != DELETE:
                logger.warning("override %s: %s %s no longer exists",
                               override["_id"], label, target_id)
                missing += 1
            else:
                unchanged += 1
            continue

        # The node was there a moment ago, but another publish or another
        # editor can remove it between the read above and the write below.
        # Losing that race is not an error worth failing a publish over.
        try:
            if override["op"] == DELETE:
                delete_node(client, label, target_id, cascade=True)
                applied += 1
                continue

            fields = override.get("fields") or {}
            if not _needs_write(current, fields):
                unchanged += 1
                continue
            covered = {name: current.get(name) for name, value in fields.items()
                       if current.get(name) != value}
            if covered:
                db[COLLECTION].update_one(
                    {"_id": override["_id"]},
                    {"$set": {f"source_value.{name}": value for name, value in covered.items()}})
            update_node(client, label, target_id, fields)
            applied += 1
        except MutationError as error:
            logger.warning("override %s skipped: %s", override["_id"], error)
            missing += 1

    if applied or missing:
        logger.info("overrides: %d applied, %d already in place, %d target(s) gone",
                    applied, unchanged, missing)
    return {"overrides_applied": applied, "overrides_unchanged": unchanged,
            "overrides_missing": missing}
