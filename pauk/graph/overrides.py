"""Manual edits that survive the pipeline.

Neo4j is a display copy, not the source of truth: `pauk publish graph`
pours prepared documents into it with `MERGE ... ON MATCH SET n +=
row.properties`. A field corrected by hand and also written by the
pipeline is overwritten on the next publish; a node deleted by hand comes
back, because MERGE creates it again.

So a manual edit is kept as a *decision*, not only as a value in the
graph: one document per target in `graph_overrides`, reapplied after
every publish and after every graph-wide dedup. The graph ends up
carrying the same edit again, which is why applying has to be idempotent:
reapplying an override that is already in place must produce no audit
entry at all, or the change feed fills with edits nobody made.

Two things this buys beyond survival:

- the manual edits are a readable list, not values dissolved in the graph;
- `auto_value` (what the field held before the edit) makes conflicts
  visible: "the source now says X, your override still says Y".

Deletion is a tombstone as much as an operation. The loader has to skip
tombstoned ids *before* writing, otherwise every run recreates the node
and every reapply deletes it again, so the graph would be correct and the
audit log would be nonsense.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from neo4j.time import DateTime
from pymongo import ReturnDocument
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError, PyMongoError

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
MAX_DECISION_WRITE_ATTEMPTS = 5

SET = "set"
DELETE = "delete"
# Preserve manually added nodes and relationships during graph pruning.
CREATE = "create"
LINK = "link"
OPERATIONS = (SET, DELETE, CREATE)


class DecisionWriteUncertain(PyMongoError):
    """Mongo may have committed a decision but its result cannot be verified."""


class DecisionWriteConflict(PyMongoError):
    """A decision changed state or exhausted its bounded write attempts."""


def _write_decision(db: Database, document_id: str,
                    build: Callable[[dict], dict | None]) -> dict | None:
    """Apply one complete decision, retrying if another editor changed it.

    A lost reply does not prove the write failed. Its unique token lets us
    confirm a commit before a caller compensates the graph mutation.
    """
    collection = db[COLLECTION]
    for _ in range(MAX_DECISION_WRITE_ATTEMPTS):
        before = collection.find_one({"_id": document_id})
        update = build(before or {})
        if update is None:
            return None
        token = uuid4().hex
        update.setdefault("$set", {}).update(write_id=token, updated_at=_now())
        query = {"_id": document_id, "write_id": (before or {}).get(
            "write_id", {"$exists": False})}
        try:
            saved = collection.find_one_and_update(
                query, update, upsert=before is None, return_document=ReturnDocument.AFTER)
        except DuplicateKeyError:
            if before is None:
                continue
            raise
        except PyMongoError as error:
            try:
                current = collection.find_one({"_id": document_id})
            except PyMongoError:
                raise DecisionWriteUncertain(document_id) from error
            if current and current.get("write_id") == token:
                return current
            if ((current or {}).get("write_id") != (before or {}).get("write_id")
                    or (before is None and current is not None)):
                raise DecisionWriteUncertain(document_id) from error
            raise
        if saved is not None:
            return saved
    raise DecisionWriteConflict(f"decision {document_id} kept changing; retry the edit")


def _now() -> datetime:
    """Current time at the precision BSON keeps.

    Mongo stores milliseconds; a plain datetime.now() carries microseconds
    and comes back rounded, so a document returned to the caller would
    differ from the one in the database.
    """
    moment = datetime.now(UTC)
    return moment.replace(microsecond=moment.microsecond // 1000 * 1000)


def _made_by_hand(row: dict | None) -> bool:
    """Whether a node decision is about a record a person added.

    The operation alone does not say: a deletion overwrites it. The `op`
    check covers decisions written before the marker existed.
    """
    return bool(row) and bool(row.get("created") or row.get("op") == CREATE)


def override_id(label: str, target_id: str) -> str:
    """Deterministic key, so editing the same node twice updates one document."""
    return f"node:{label}:{target_id}"


def relationship_override_id(src_label: str, rel_type: str, tgt_label: str,
                             src_id: str, tgt_id: str) -> str:
    """Key for a decision about one relationship.

    A relationship needs five parts to be named: a node is one id, an edge
    is a type plus both ends. The target id is whatever the loader matches
    the target by (`url` for a Repository, `login` for a GitHubProfile),
    so the key matches what the publish path actually compares.
    """
    return f"rel:{src_label}:{rel_type}:{tgt_label}:{src_id}:{tgt_id}"


def override_generation(db: Database, label: str, target_id: str) -> int:
    """Read before editing the graph to detect a later undo or deletion."""
    row = db[COLLECTION].find_one({"_id": override_id(label, target_id)}, {"generation": True})
    return (row or {}).get("generation", 0)


def _graph_version(value: object) -> str:
    """Keep Neo4j's nanoseconds and normalize offsets for string comparison."""
    if isinstance(value, DateTime):
        version = value
    elif isinstance(value, datetime):
        version = DateTime.from_native(value)
    else:
        version = DateTime.from_iso_format(str(value))
    if version.tzinfo is None:
        version = version.replace(tzinfo=UTC)
    return version.as_timezone(UTC).iso_format()


def _field_updates(existing: dict, fields: dict, auto_value: dict, version: str | None) -> dict:
    """Keep the newest field values and the oldest known original values."""
    values = dict(existing.get("fields") or {})
    originals = dict(existing.get("auto_value") or {})
    versions = dict(existing.get("field_versions") or {})
    first_versions = dict(existing.get("auto_value_versions") or {})
    for name, value in fields.items():
        if version is None or name not in versions or version > versions[name]:
            values[name] = value
            if version is None:
                versions.pop(name, None)
            else:
                versions[name] = version
    for name, value in auto_value.items():
        if (name not in originals or (version is not None and name in first_versions
                                     and version < first_versions[name])):
            originals[name] = value
            if version is None:
                first_versions.pop(name, None)
            else:
                first_versions[name] = version
    return {"fields": values, "auto_value": originals,
            "field_versions": versions, "auto_value_versions": first_versions}


def record_override(db: Database, label: str, target_id: str, op: str,
                    fields: dict | None = None, actor: str = "unknown",
                    note: str = "", auto_value: dict | None = None,
                    snapshot: dict | None = None, graph_updated_at: object | None = None,
                    expected_generation: int | None = None) -> dict:
    """Write down a manual decision about one node.

    Fields are merged into whatever the document already holds rather than
    replacing it: two people editing different fields of the same person
    on different days must both survive, and a replace would silently drop
    the earlier edit.

    Args:
        db: Mongo database.
        label: Node label, checked against the whitelist.
        target_id: Node id the decision is about.
        op: "set", "delete" or "create".
        fields: Field values for "set" and "create"; ignored for "delete".
        actor: Who decided, for the audit trail.
        note: Free-text reason, shown in the panel.
        auto_value: Value before the earliest graph edit. An older delayed
            edit corrects an intermediate value recorded by a newer edit;
            a legacy original without a version is preserved.
        snapshot: For a deletion, every field the node carried. The
            decision then holds what is needed to put the record back, and
            restoring stops depending on the audit feed, which records
            history, not state, and summarises a bulk operation without
            listing fields at all.
        graph_updated_at: Version returned by the graph field update. Each
            field follows this order even if Mongo receives edits out of order.
        expected_generation: State read before editing the graph. Undo,
            deletion or creation changes it and rejects the delayed edit.

    Returns:
        The stored document.
    """
    if op not in OPERATIONS:
        raise UnknownEntity(f"unknown override operation: {op!r} (known: {', '.join(OPERATIONS)})")
    validate_label(label)
    fields = dict(fields or {})
    if op in (SET, CREATE):
        validate_fields(label, fields)
        if op == SET and not fields:
            raise MutationError("a 'set' override with no fields changes nothing")

    document_id = override_id(label, target_id)
    version = _graph_version(graph_updated_at) if graph_updated_at is not None else None

    def build(existing: dict) -> dict:
        generation = existing.get("generation", 0)
        if expected_generation is not None and generation != expected_generation:
            raise DecisionWriteConflict(f"decision {document_id} was withdrawn or replaced")
        fresh = op == SET and not existing.get("active")
        base = {} if fresh else existing
        latest = version is None or version >= base.get("graph_version", "")
        update: dict = {
            "$set": {
                "kind": "node", "label": label, "target_id": target_id,
                "generation": generation + 1 if op != SET else generation,
                **_field_updates(base, fields, auto_value or {}, version),
            },
            "$setOnInsert": {"created_at": _now()},
        }
        if fresh:
            update["$set"]["source_value"] = {}
        if latest:
            update["$set"].update(
                actor=actor, active=True,
                op=CREATE if op == SET and existing.get("op") == CREATE else op)
            if version is not None:
                update["$set"]["graph_version"] = version
            else:
                update.setdefault("$unset", {})["graph_version"] = ""
            if note:
                update["$set"]["note"] = note
            else:
                update["$setOnInsert"]["note"] = ""
        if op != SET:
            update["$set"].update(field_versions={}, auto_value_versions={})
        if op == CREATE or _made_by_hand(existing):
            update["$set"]["created"] = True
        if snapshot is not None:
            update["$set"]["snapshot"] = snapshot
        elif op == CREATE:
            update.setdefault("$unset", {})["snapshot"] = ""
        return update

    stored = _write_decision(db, document_id, build)
    logger.info("override recorded: %s %s (%s)", op, document_id, ", ".join(sorted(fields)))
    return cast(dict, stored)


def record_relationship_override(db: Database, src_label: str, rel_type: str, tgt_label: str,
                                 src_id: str, tgt_id: str, op: str = DELETE,
                                 actor: str = "unknown", note: str = "",
                                 snapshot: dict | None = None) -> dict:
    """Write down a manual decision about one relationship.

    Two kinds, and they are not symmetrical. `delete` is an instruction:
    the loader would recreate the edge from the same prepared row on every
    run, and this is what stops it. `link` is only a claim: the edge is
    there already and publishing leaves it alone, but without it nothing
    tells an edge somebody added by hand from one the pipeline made and has
    since stopped making, and a prune would remove both.

    Args:
        snapshot: Properties read before unlinking, so undo restores the
            edge's data as well as its endpoints. Used only for deletion.

    Raises:
        UnknownEntity: The triple is not a relationship the graph has, or
            the operation is neither of the two.
    """
    if op not in (DELETE, LINK):
        raise UnknownEntity(
            f"relationship overrides support {DELETE!r} and {LINK!r}, got {op!r}")
    validate_relationship(src_label, rel_type, tgt_label)
    document_id = relationship_override_id(src_label, rel_type, tgt_label, src_id, tgt_id)

    def build(existing: dict) -> dict:
        return {"$set": {
            "kind": "rel", "src_label": src_label, "rel_type": rel_type,
            "tgt_label": tgt_label, "src_id": src_id, "target_id": tgt_id,
            "op": op, "snapshot": dict(snapshot or {}) if op == DELETE else {},
            "actor": actor, "note": note or existing.get("note", ""), "active": True,
        }, "$setOnInsert": {"created_at": _now()}}

    stored = _write_decision(db, document_id, build)
    logger.info("override recorded: %s (%s %s)-[:%s]->(%s %s)",
                "unlink" if op == DELETE else "link",
                src_label, src_id, rel_type, tgt_label, tgt_id)
    return cast(dict, stored)


def deactivate_relationship_override(db: Database, src_label: str, rel_type: str, tgt_label: str,
                                     src_id: str, tgt_id: str,
                                     only_op: str | None = None) -> bool:
    """Stop keeping a relationship unlinked; the next publish restores it.

    Args:
        only_op: Act only on a decision of this kind. Both kinds share one
            document id, so "restore the link somebody removed" would
            otherwise withdraw the claim on a link somebody added and
            report that a publish will rebuild it.
    """
    document_id = relationship_override_id(src_label, rel_type, tgt_label, src_id, tgt_id)

    def build(existing: dict) -> dict | None:
        if not existing.get("active") or (only_op is not None and existing.get("op") != only_op):
            return None
        return {"$set": {"active": False}}

    return _write_decision(db, document_id, build) is not None


def claimed_relationships(db: Database) -> set[tuple[str, str, str, str, str]]:
    """Relationships added by hand that automatic synchronization must preserve."""
    return {
        (row["src_label"], row["rel_type"], row["tgt_label"], row["src_id"], row["target_id"])
        for row in db[COLLECTION].find({"active": True, "kind": "rel", "op": LINK})
    }


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


def deactivate_override(db: Database, label: str, target_id: str,
                        only_op: str | None = None) -> bool:
    """Stop applying an override without losing the record of it.

    Undoing an edit is switching this off and reapplying, not deleting the
    document: the panel still has to show that the edit existed and who
    made it.

    Withdrawing a deletion from a node that was also edited leaves the edit
    standing: one document holds both, and switching the whole thing off
    would drop a field correction, and the next publish would overwrite
    the value again.

    Args:
        db: Mongo database.
        label: Node label.
        target_id: Which node.
        only_op: Withdraw only if the decision is this operation. Creating
            a node by hand withdraws the tombstone that would delete it
            again, and nothing else. Left unset, the decision is withdrawn
            whatever it says.

    Returns:
        Whether anything was withdrawn.
    """
    document_id = override_id(label, target_id)

    def build(row: dict) -> dict | None:
        if not row.get("active") or (only_op is not None and row.get("op") != only_op):
            return None
        update: dict = {"$set": {"generation": row.get("generation", 0) + 1,
                           "field_versions": {}, "auto_value_versions": {}},
                  "$unset": {"graph_version": ""}}
        if row.get("op") == DELETE and (row.get("fields") or _made_by_hand(row)):
            update["$set"]["op"] = CREATE if _made_by_hand(row) else SET
            update["$unset"]["snapshot"] = ""
        else:
            update["$set"]["active"] = False
        return update

    return _write_decision(db, document_id, build) is not None


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
    the end of a publish and at the end of a graph-wide dedup (both of
    which can undo manual work) and by `pauk overrides apply`.

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
        if override.get("op") == LINK:
            # A claim, not an instruction: only a prune reads these.
            unchanged += 1
            continue
        if override.get("kind") == "rel":
            # The loader skips these; an edge made by anything else goes here.
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
            # A deletion whose node is already gone is the steady state.
            if override["op"] != DELETE:
                logger.warning("override %s: %s %s no longer exists",
                               override["_id"], label, target_id)
                missing += 1
            else:
                unchanged += 1
            continue

        # The node can go between the read above and the write below.
        try:
            if override["op"] == DELETE:
                delete_node(client, label, target_id, cascade=True)
                applied += 1
                continue

            # A hand-made record carries the same kind of row as a hand edit.
            fields = override.get("fields") or {}
            if not fields or not _needs_write(current, fields):
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
