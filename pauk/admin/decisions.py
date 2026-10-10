"""Manual decisions in force, and where they disagree with the source.

Every hand edit is kept as a decision in `graph_overrides` and reapplied after
each publish and dedup. A conflict is not "the graph differs from the override"
(it never does) but a later pipeline write that differs from `auto_value`, the
value the field held before a person changed it.
"""

from __future__ import annotations

from pymongo.database import Database

from pauk.admin import feed
from pauk.graph.overrides import COLLECTION, CREATE, DELETE, LINK, SET, active_overrides

PAGE = 50

# Writes the panel makes itself; anything else is the pipeline's own opinion.
PANEL = "admin-ui"


def _moment(value) -> str:
    """A time as text in isoformat, the shape the feed uses.

    `str(datetime)` puts a space where isoformat puts "T", and the two would
    sort wrongly together as text.
    """
    if value is None:
        return ""
    return value.isoformat() if hasattr(value, "isoformat") else str(value).replace(" ", "T", 1)


#: What each kind of decision is called on the page, by (is it a link, op).
WORDS = {
    (True, DELETE): "связь удалена",
    (True, LINK): "связь добавлена вручную",
    (False, SET): "поля изменены",
    (False, DELETE): "запись удалена",
    (False, CREATE): "запись заведена вручную",
}


def _title(row: dict) -> str:
    """The decision as one line, for a list."""
    if row.get("kind") == "rel":
        return (f"({row['src_label']} {row['src_id']})"
                f"-[:{row['rel_type']}]->({row['tgt_label']} {row['target_id']})")
    return f"{row['label']} {row['target_id']}"


def in_force(db: Database, limit: int = PAGE, skip: int = 0) -> list[dict]:
    """Decisions currently applied, newest first.
    Paged in the database so the panel does not load every decision ever kept.

    Returns:
        Rows as stored, with `title` for display and `what` describing the
        operation in the panel's words.
    """
    rows = list(db[COLLECTION].find({"active": True})
                .sort("updated_at", -1).skip(skip).limit(limit))
    for row in rows:
        row["title"] = _title(row)
        row["what"] = WORDS.get((row.get("kind") == "rel", row.get("op")), "решение")
        # A claim is taken back by removing the thing, not by undoing.
        row["undoable"] = row.get("op") not in (CREATE, LINK)
        row["pairs"] = sorted(
            (name, (row.get("auto_value") or {}).get(name), value)
            for name, value in (row.get("fields") or {}).items())
    return rows


def conflicts(db: Database, limit: int | None = PAGE, skip: int = 0) -> list[dict]:
    """Fields where the source now says something other than it used to.

    For every hand-edited field, looks for a later non-panel write in the feed
    whose value differs from `auto_value`.

    Args:
        db: Mongo database.
        limit: Rows to return; None for all, which gives the page its total
            without a second pass.
        skip: Rows to skip, for paging.

    Returns:
        One row per field in disagreement: the decision, what a person set,
        what the source used to say, what it says now, and who wrote that.
    """
    edits = [row for row in active_overrides(db)
             if row.get("kind") != "rel" and row.get("op") == SET]
    writes = _source_writes(db, edits)

    found = []
    for row in edits:
        auto = row.get("auto_value") or {}
        since = row.get("created_at")
        stated = row.get("source_value") or {}
        for name, ours in (row.get("fields") or {}).items():
            if name in stated:
                # Recorded by apply_overrides when it covered the value up.
                value, actor, when = stated[name], "pipeline", _moment(row.get("updated_at"))
            else:
                latest = writes.get((row["label"], row["target_id"], name))
                if latest is None or (since is not None and latest[2] <= _moment(since)):
                    continue
                value, actor, when = latest
            if value == auto.get(name):
                continue
            found.append({
                "label": row["label"], "target_id": row["target_id"], "title": _title(row),
                "field": name, "ours": ours, "was": auto.get(name), "now": value,
                "actor": actor, "when": when, "note": row.get("note", ""),
            })

    found.sort(key=lambda row: row["when"], reverse=True)
    if limit is None:
        return found[skip:]
    return found[skip:skip + limit]


def _source_writes(db: Database, edits: list[dict]) -> dict[tuple[str, str, str], tuple]:
    """The latest non-panel write to each hand-edited field, in one query.

    Returns:
        (label, node_id, field) -> (value now, who wrote it, when).
    """
    if not edits:
        return {}
    wanted = {(row["label"], row["target_id"]) for row in edits}
    # Only fields somebody edited by hand; the rest move all the time.
    fields = {name for row in edits for name in (row.get("fields") or {})}
    rows = db[feed.COLLECTION].find(
        {"entity_type": {"$in": sorted({label for label, _ in wanted})},
         "entity_id": {"$in": sorted({node_id for _, node_id in wanted})},
         "source": {"$ne": PANEL}},
        {"entity_type": True, "entity_id": True, "timestamp": True,
         "actor": True, "diff": True})

    # Newest per field kept while walking: such a sort leans on no index.
    latest: dict[tuple[str, str, str], tuple] = {}
    for entry in rows:
        entity = (entry.get("entity_type"), entry.get("entity_id"))
        if entity not in wanted:
            # The two $in lists cross more pairs than exist.
            continue
        when = entry.get("timestamp", "")
        for name, pair in (entry.get("diff") or {}).items():
            if name not in fields or not pair:
                continue
            key = (*entity, name)
            if key not in latest or when > latest[key][2]:
                latest[key] = (pair[1], entry.get("actor", "?"), when)
    return latest


def count_conflicts(db: Database) -> int:
    """How many disagreements there are in total.

    A page that needs both the count and a slice should call
    `conflicts(db, limit=None)` once instead.
    """
    return len(conflicts(db, limit=None))


def count_in_force(db: Database) -> int:
    return db[COLLECTION].count_documents({"active": True})


def deleted_fields(db: Database, label: str, node_id: str) -> dict:
    """What a deleted record held, for putting it back.

    Read from the snapshot in the decision, with the feed as a fallback for
    records deleted before snapshots existed. Call it before withdrawing the
    deletion, which drops the snapshot.
    """
    row = db[COLLECTION].find_one({"_id": f"node:{label}:{node_id}", "op": "delete"})
    if row and row.get("snapshot"):
        return dict(row["snapshot"])
    return feed.deleted_state(db, label, node_id)


def source_of_truth(db: Database, label: str, node_id: str) -> dict:
    """What the pipeline says a hand-edited record's fields should hold.

    Used when an edit is withdrawn, so the field goes back to the source's
    value at once. Prefers `source_value` (written by `apply_overrides`), then
    `auto_value`.
    """
    row = db[COLLECTION].find_one({"_id": f"node:{label}:{node_id}"})
    if row is None:
        return {}
    stated, before = row.get("source_value") or {}, row.get("auto_value") or {}
    # Only fields the source has spoken about: None would erase, not restore.
    return {name: stated.get(name, before.get(name))
            for name in (row.get("fields") or {})
            if name in stated or name in before}
