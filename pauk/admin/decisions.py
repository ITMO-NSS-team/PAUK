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

# Writes the panel makes itself. Anything else changing the same field is
# the pipeline having its own opinion.
PANEL = "admin-ui"


def _title(row: dict) -> str:
    """The decision as one line, for a list."""
    if row.get("kind") == "rel":
        return (f"({row['src_label']} {row['src_id']})"
                f"-[:{row['rel_type']}]->({row['tgt_label']} {row['target_id']})")
    return f"{row['label']} {row['target_id']}"


def in_force(db: Database) -> list[dict]:
    """Decisions currently applied, newest first.

    Returns:
        Rows as stored, with `title` for display and `what` describing the
        operation in the panel's words.
    """
    rows = sorted(active_overrides(db), key=lambda row: row.get("updated_at") or "", reverse=True)
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


def conflicts(db: Database) -> list[dict]:
    """Fields where the source now says something other than it used to.

    For every hand-edited field, the feed is searched for a later write
    made by anything but the panel. If that write set a value different
    from `auto_value`, the pipeline has changed its mind about the field
    and the person's edit is now hiding a fact rather than a mistake.

    Returns:
        One row per field in disagreement: the decision it belongs to,
        what a person set, what the source used to say, what it says now,
        and who wrote that.
    """
    found = []
    for row in active_overrides(db):
        if row.get("kind") == "rel" or row.get("op") != SET:
            continue
        auto = row.get("auto_value") or {}
        since = row.get("created_at")
        for name, ours in (row.get("fields") or {}).items():
            latest = _last_source_write(db, row["label"], row["target_id"], name, since)
            if latest is None:
                continue
            value, actor, when = latest
            if value == auto.get(name):
                continue        # источник повторяет то же, что и был — не конфликт
            found.append({
                "label": row["label"], "target_id": row["target_id"], "title": _title(row),
                "field": name, "ours": ours, "was": auto.get(name), "now": value,
                "actor": actor, "when": when, "note": row.get("note", ""),
            })
    return sorted(found, key=lambda row: row["when"], reverse=True)


def _last_source_write(db: Database, label: str, node_id: str, field: str,
                       since) -> tuple[object, str, str] | None:
    """The most recent non-panel write to one field, or None."""
    query: dict = {"entity_type": label, "entity_id": node_id,
                   "source": {"$ne": PANEL}, f"diff.{field}": {"$exists": True}}
    if since is not None:
        query["timestamp"] = {"$gt": since.isoformat() if hasattr(since, "isoformat") else since}
    row = db[feed.COLLECTION].find_one(query, sort=[("timestamp", -1)])
    if row is None:
        return None
    pair = (row.get("diff") or {}).get(field)
    if not pair:
        return None
    return pair[1], row.get("actor", "?"), row.get("timestamp", "")


def count_conflicts(db: Database) -> int:
    return len(conflicts(db))


def count_in_force(db: Database) -> int:
    return db[COLLECTION].count_documents({"active": True})
