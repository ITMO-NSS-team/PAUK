"""Reading the change feed: who edited what, and when.

Every write through `AuditedNeo4jClient` lands in the `audit` collection,
including the ones a publish or a dedup makes — the feed is not only about
the panel. That is the point of showing it here: a field that keeps
changing back is a conflict between a person and the pipeline, and it is
visible only when both are in one list.

Reading, and one write: `trim`, which is the only thing that ever removes
an entry. Nothing here edits the graph.
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

# What the entries look like, in the panel's words.
KINDS = {
    "created": "создано",
    "updated": "изменено",
    "deleted": "удалено",
    "bulk": "массово",
}


def _query(*, actor: str = "", entity_type: str = "", entity_id: str = "",
           kind: str = "", since: str = "", until: str = "") -> dict:
    """The filter behind both a page of the feed and its counter.

    Built in one place because the names a caller uses are not the names in
    the documents: `kind` is stored as `change_kind`, and a page and a total
    that each did their own translating would answer different questions.
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
        oldest_first: Read the feed forwards instead of backwards. What
            happened first is what somebody retracing a run wants.
        **filters: See `_query` — who, what, which record, what kind of
            change, and between which dates.

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
    """Everything that happened to one entity, newest first.

    Shown on the node's own page, where the question is "why does this
    field say that" rather than "what happened today".
    """
    return entries(db, entity_type=entity_type, entity_id=entity_id, limit=limit)


def deleted_state(db: Database, entity_type: str, entity_id: str) -> dict:
    """The fields an entity had when it was last deleted.

    A deletion is recorded as `{field: (value, None)}` for everything the
    node carried, so the feed holds enough to put it back exactly as it
    was. Returns an empty dict when the last thing that happened was not a
    deletion — restoring then would overwrite something that is alive.
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

    The feed is the one thing here that only ever grows: every edit writes a
    line and every publish a summary per batch, and until now nothing
    removed any of it.

    What it costs to lose is bounded. A record deleted by hand is restored
    from the snapshot kept in its own decision (`graph_overrides`), which
    this does not touch — the feed is only the fallback for records deleted
    before those snapshots existed. Everything else here is read to be
    looked at, not acted on.

    Args:
        before: ISO timestamp. Entries stamped earlier go. Text comparison
            is time comparison, which is why the entries are stamped in ISO
            in the first place.
        apply: False counts what would go and changes nothing, so the size
            of the cut can be seen before it is made.

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
