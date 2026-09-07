"""Reading the change feed: who edited what, and when.

Every write through `AuditedNeo4jClient` lands in the `audit` collection,
including the ones a publish or a dedup makes — the feed is not only about
the panel. That is the point of showing it here: a field that keeps
changing back is a conflict between a person and the pipeline, and it is
visible only when both are in one list.

Reading only. Nothing in this module edits the graph or the feed itself.
"""

from __future__ import annotations

from pymongo.database import Database

COLLECTION = "audit"
PAGE = 50

# What the entries look like, in the panel's words. `operation` is the
# client method that made the change, which says nothing to a reader.
KINDS = {
    "created": "создано",
    "updated": "изменено",
    "deleted": "удалено",
    "bulk": "массово",
}


def entries(db: Database, *, actor: str = "", entity_type: str = "", entity_id: str = "",
            kind: str = "", limit: int = PAGE, skip: int = 0) -> list[dict]:
    """One page of the feed, newest first.

    Args:
        db: Mongo database.
        actor: Filter by who made the change, exactly as recorded
            (`user:ivanov`, `pipeline`, and so on).
        entity_type: Filter by node label, or by the `(A)-[:REL]->(B)`
            shape a relationship is recorded under.
        entity_id: Filter by the id of one entity — the history of a
            single node.
        kind: created | updated | deleted | bulk.
        limit: Rows per page.
        skip: Rows to skip, for paging.

    Returns:
        Rows as stored, with `kind_ru` added for display.
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

    rows = list(db[COLLECTION].find(query).sort("timestamp", -1).skip(skip).limit(limit))
    for row in rows:
        row["kind_ru"] = KINDS.get(row.get("change_kind", ""), row.get("change_kind", ""))
        # Stored as {field: [old, new]}; a template reads pairs more easily
        # than a mapping, and the order should be stable between renders.
        row["changes"] = sorted((row.get("diff") or {}).items())
    return rows


def count(db: Database, **filters) -> int:
    """How many entries match, for the pager."""
    query = {name: value for name, value in filters.items() if value}
    return db[COLLECTION].count_documents(query)


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
