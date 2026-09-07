"""Manual decisions in force, and where they disagree with the source.

Every hand edit is kept as a decision in `graph_overrides`, reapplied
after each publish and each dedup. This module reads that collection for
the panel: what is in force, and which of it the pipeline has since
contradicted.

A conflict is not "the graph differs from the override" — the override is
reapplied, so the graph always agrees with it. It is the *source* that
moves: `auto_value` records what the field held before a person changed
it, and when a later run writes something different from that, the
pipeline is saying the value has changed for a reason of its own. Nobody
sees that unless it is looked for, which is what this does.
"""

from __future__ import annotations

from pymongo.database import Database

from pauk.admin import feed
from pauk.graph.overrides import COLLECTION, SET, active_overrides

PAGE = 50

# Writes the panel makes itself. Anything else changing the same field is
# the pipeline having its own opinion.
PANEL = "admin-ui"


def _moment(value) -> str:
    """A time as text, in the same shape the feed uses.

    The feed stores isoformat strings; `updated_at` is a datetime, and its
    str() puts a space where isoformat puts "T". Sorting the two together
    as text orders them wrongly, so everything is brought to isoformat.
    """
    if value is None:
        return ""
    return value.isoformat() if hasattr(value, "isoformat") else str(value).replace(" ", "T", 1)


def _title(row: dict) -> str:
    """The decision as one line, for a list."""
    if row.get("kind") == "rel":
        return (f"({row['src_label']} {row['src_id']})"
                f"-[:{row['rel_type']}]->({row['tgt_label']} {row['target_id']})")
    return f"{row['label']} {row['target_id']}"


def in_force(db: Database, limit: int = PAGE, skip: int = 0) -> list[dict]:
    """Decisions currently applied, newest first.

    Paged in the database rather than in Python: the list grows with every
    hand edit that is never withdrawn, and a panel that loads all of them
    to show fifty would get slower for as long as the project runs.

    Returns:
        Rows as stored, with `title` for display and `what` describing the
        operation in the panel's words.
    """
    rows = list(db[COLLECTION].find({"active": True})
                .sort("updated_at", -1).skip(skip).limit(limit))
    for row in rows:
        row["title"] = _title(row)
        if row.get("kind") == "rel":
            row["what"] = "связь удалена"
        elif row.get("op") == SET:
            row["what"] = "поля изменены"
        else:
            row["what"] = "запись удалена"
        row["pairs"] = sorted(
            (name, (row.get("auto_value") or {}).get(name), value)
            for name, value in (row.get("fields") or {}).items())
    return rows


def conflicts(db: Database, limit: int | None = PAGE, skip: int = 0) -> list[dict]:
    """Fields where the source now says something other than it used to.

    For every hand-edited field, the feed is searched for a later write
    made by anything but the panel. If that write set a value different
    from `auto_value`, the pipeline has changed its mind about the field
    and the person's edit is now hiding a fact rather than a mistake.

    Args:
        db: Mongo database.
        limit: Rows to return; None for all of them, which is how the page
            gets its own count without walking every decision a second
            time.
        skip: Rows to skip, for paging.

    Returns:
        One row per field in disagreement: the decision it belongs to,
        what a person set, what the source used to say, what it says now,
        and who wrote that.
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
                # Recorded by apply_overrides at the moment it covered the
                # value up — the source's own word, without inference.
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

    # Sorted on one representation: `updated_at` is a datetime whose str()
    # separates date and time with a space, while feed timestamps use "T".
    # A space sorts before "T", so mixing them sent every decision-sourced
    # row to the bottom regardless of when it happened.
    found.sort(key=lambda row: row["when"], reverse=True)
    if limit is None:
        return found[skip:]
    return found[skip:skip + limit]


def _source_writes(db: Database, edits: list[dict]) -> dict[tuple[str, str, str], tuple]:
    """The latest non-panel write to each hand-edited field, in one query.

    Asked one decision at a time this was a round trip per field, so a page
    listing fifty decisions cost hundreds of them and grew with every edit
    anybody ever made. The entities are known up front, so they are fetched
    together and the newest write per field is picked while walking the
    result.

    Returns:
        (label, node_id, field) -> (value now, who wrote it, when).
    """
    if not edits:
        return {}
    wanted = {(row["label"], row["target_id"]) for row in edits}
    # Only the fields somebody edited by hand; a node's other fields move
    # all the time and say nothing about a decision.
    fields = {name for row in edits for name in (row.get("fields") or {})}
    rows = db[feed.COLLECTION].find(
        {"entity_type": {"$in": sorted({label for label, _ in wanted})},
         "entity_id": {"$in": sorted({node_id for _, node_id in wanted})},
         "source": {"$ne": PANEL}},
        {"entity_type": True, "entity_id": True, "timestamp": True,
         "actor": True, "diff": True})

    # The newest per field is kept while walking, rather than asking the
    # database to sort: a sort across several entities cannot lean on the
    # (entity_type, entity_id, timestamp) index, and Mongo gives up on an
    # in-memory sort past 32 MB. One pass needs neither.
    latest: dict[tuple[str, str, str], tuple] = {}
    for entry in rows:
        entity = (entry.get("entity_type"), entry.get("entity_id"))
        if entity not in wanted:
            # The two $in lists cross more pairs than exist: a label from
            # one decision and an id from another match nothing real.
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

    There is no cheaper way than looking: a conflict is a comparison
    between a decision and what the source said afterwards, not a flag on a
    document. So a page that needs both the number and a slice should call
    `conflicts(db, limit=None)` once and use its length, rather than this.
    """
    return len(conflicts(db, limit=None))


def count_in_force(db: Database) -> int:
    return db[COLLECTION].count_documents({"active": True})


def deleted_fields(db: Database, label: str, node_id: str) -> dict:
    """What a deleted record held, for putting it back.

    Read from the decision itself: both the panel and `pauk admin node
    delete` take a snapshot before removing the node. The feed is only a
    fallback, for records deleted before snapshots existed — it stores
    history rather than state, and a bulk operation lands there as a
    summary with no fields at all.

    Call it before withdrawing the deletion, not after: withdrawing drops
    the snapshot along with the decision it belongs to.
    """
    row = db[COLLECTION].find_one({"_id": f"node:{label}:{node_id}", "op": "delete"})
    if row and row.get("snapshot"):
        return dict(row["snapshot"])
    return feed.deleted_state(db, label, node_id)


def source_of_truth(db: Database, label: str, node_id: str) -> dict:
    """What the pipeline says a hand-edited record's fields should hold.

    Used when an edit is withdrawn: the field has to go back to the
    source's value there and then, rather than wait for a publish to
    overwrite it — a record the pipeline no longer covers would keep the
    hand-written value indefinitely.

    Prefers `source_value`, written by `apply_overrides` at the moment it
    covered the value up, and falls back to `auto_value`, recorded when
    the edit was first made.
    """
    row = db[COLLECTION].find_one({"_id": f"node:{label}:{node_id}"})
    if row is None:
        return {}
    stated, before = row.get("source_value") or {}, row.get("auto_value") or {}
    # Only fields the source has actually spoken about. A field with
    # neither record is left as it is: writing None there would erase a
    # value on the strength of not knowing anything about it.
    return {name: stated.get(name, before.get(name))
            for name in (row.get("fields") or {})
            if name in stated or name in before}
