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
    read_node,
    update_node,
    validate_fields,
    validate_label,
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


def record_override(db: Database, label: str, target_id: str, op: str,
                    fields: dict | None = None, actor: str = "unknown",
                    note: str = "", auto_value: dict | None = None) -> dict:
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
    existing = db[COLLECTION].find_one({"_id": document_id}) or {}
    merged_fields = {**(existing.get("fields") or {}), **fields}
    known_auto = existing.get("auto_value") or {}
    fresh_auto = {name: value for name, value in (auto_value or {}).items()
                  if name not in known_auto}

    document = {
        "_id": document_id,
        "kind": "node",
        "label": label,
        "target_id": target_id,
        "op": op,
        "fields": merged_fields,
        "auto_value": {**known_auto, **fresh_auto},
        "actor": actor,
        "note": note or existing.get("note", ""),
        "active": True,
        "created_at": existing.get("created_at", now),
        "updated_at": now,
    }
    db[COLLECTION].replace_one({"_id": document_id}, document, upsert=True)
    logger.info("override recorded: %s %s (%s)", op, document_id, ", ".join(sorted(merged_fields)))
    # Read back rather than return what was sent: the driver hands
    # timestamps back without a timezone, so the two would differ in type
    # depending on whether this was the first edit or a later one.
    return db[COLLECTION].find_one({"_id": document_id})


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

        if override["op"] == DELETE:
            delete_node(client, label, target_id, cascade=True)
            applied += 1
            continue

        fields = override.get("fields") or {}
        if not _needs_write(current, fields):
            unchanged += 1
            continue
        update_node(client, label, target_id, fields)
        applied += 1

    if applied or missing:
        logger.info("overrides: %d applied, %d already in place, %d target(s) gone",
                    applied, unchanged, missing)
    return {"overrides_applied": applied, "overrides_unchanged": unchanged,
            "overrides_missing": missing}
