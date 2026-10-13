"""Reading the change feed: who edited what, and when.

Every write through `AuditedNeo4jClient` lands in the `audit` collection,
including publish and dedup writes, so a field flipping between a person and
the pipeline shows up in one list. `trim` is the only writer.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from pymongo.database import Database

logger = logging.getLogger("pauk.admin")

COLLECTION = "audit"
PAGE = 50

#: How much history `trim` keeps by default.
KEEP_DAYS = 180

KINDS = {
    "created": "создано",
    "updated": "изменено",
    "deleted": "удалено",
    "bulk": "массово",
}


def _query(*, actor: str = "", entity_type: str = "", entity_id: str = "",
           kind: str = "", since: str = "", until: str = "") -> dict:
    """The filter behind both a page of the feed and its counter.

    Caller names differ from stored ones (`kind` is `change_kind`), so the
    translation lives in one place.
    """
    query: dict = {}
    if actor:
        query["actor"] = actor
    if entity_type:
        query["entity_type"] = entity_type
    if entity_id:
        query["entity_id"] = entity_id
    if kind:
        query["change_kind"] = kind
    # ISO 8601 strings: text order is time order. `until` covers its whole day.
    if since or until:
        window = {}
        if since:
            window["$gte"] = since
        if until:
            window["$lte"] = f"{until}T23:59:59"
        query["timestamp"] = window
    return query


def entries(db: Database, *, limit: int = PAGE, skip: int = 0,
            oldest_first: bool = False, **filters) -> list[dict]:
    """One page of the feed, newest first unless asked otherwise.

    Args:
        db: Mongo database.
        limit: Rows per page.
        skip: Rows to skip, for paging.
        oldest_first: Read the feed forwards instead of backwards.
        **filters: See `_query`.

    Returns:
        Rows as stored, with `kind_ru` added for display.
    """
    order = 1 if oldest_first else -1
    rows = list(db[COLLECTION].find(_query(**filters))
                .sort("timestamp", order).skip(skip).limit(limit))
    for row in rows:
        row["kind_ru"] = KINDS.get(row.get("change_kind", ""), row.get("change_kind", ""))
        # Pairs rather than a mapping, in an order stable between renders.
        row["changes"] = sorted((row.get("diff") or {}).items())
    return rows


def count(db: Database, **filters) -> int:
    """How many entries match, for the pager. Same filters as `entries`."""
    return db[COLLECTION].count_documents(_query(**filters))


def actors(db: Database) -> list[str]:
    """Everyone who has ever changed anything, for the filter list."""
    return sorted(db[COLLECTION].distinct("actor"))


def entity_types(db: Database) -> list[str]:
    """Labels and relationship shapes seen in the feed, for the filter list."""
    return sorted(db[COLLECTION].distinct("entity_type"))


def history(db: Database, entity_type: str, entity_id: str, limit: int = PAGE) -> list[dict]:
    """Everything that happened to one entity, newest first."""
    return entries(db, entity_type=entity_type, entity_id=entity_id, limit=limit)


def deleted_state(db: Database, entity_type: str, entity_id: str) -> dict:
    """The fields an entity had when it was last deleted.

    A deletion is recorded as `{field: (value, None)}`. Returns an empty dict
    when the last entry is not a deletion, so a live record is never overwritten.
    """
    row = db[COLLECTION].find_one(
        {"entity_type": entity_type, "entity_id": entity_id}, sort=[("timestamp", -1)])
    if row is None or row.get("change_kind") != "deleted":
        return {}
    return {name: pair[0] for name, pair in (row.get("diff") or {}).items()
            if pair and pair[0] is not None}


def older_than(days: int) -> str:
    """The cutoff `trim` takes, in the form the entries are stamped with."""
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def trim(db: Database, before: str, apply: bool = False) -> dict[str, int]:
    """Drop entries older than a cutoff.

    Records deleted by hand are restored from the snapshot in their own
    decision (`graph_overrides`), which this does not touch; the feed is only
    the fallback for deletions that predate those snapshots.

    Args:
        before: ISO timestamp; entries stamped earlier go.
        apply: False only counts what would go.

    Returns:
        How many entries matched and how many were removed.
    """
    query = {"timestamp": {"$lt": before}}
    matched = db[COLLECTION].count_documents(query)
    if not apply:
        return {"audit_matched": matched, "audit_removed": 0}
    removed = db[COLLECTION].delete_many(query).deleted_count
    logger.info("audit: %d entr(y/ies) older than %s removed", removed, before)
    return {"audit_matched": matched, "audit_removed": removed}
