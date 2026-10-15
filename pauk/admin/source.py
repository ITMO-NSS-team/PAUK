"""What the pipeline decided about one record, run by run.

Reads the `revisions` archive of previous prepared rows (written by
`PreparedStore`, shortened by `pauk admin trim`). The audit journal answers
"who changed the ORCID"; this answers "which run brought it".
"""

from __future__ import annotations

from typing import Any

from pymongo.database import Database

from pauk.graph.load import ENTITY_FILES, FILE_LABELS
from pauk.storage.prepared import REVISIONS, PreparedStore

PAGE = 10

#: Node label to the prepared entity its rows come from. LinkCandidate has none.
ENTITIES = {
    FILE_LABELS[filename]: entity
    for entity, filename in ENTITY_FILES.items()
    if filename in FILE_LABELS
}

#: How a row was stored, not what a run decided: `_processing` changes always.
BOOKKEEPING = frozenset({"_id", "_version", "groups", "_processing"})


def _shown(value: Any) -> Any:
    """One field value, short enough to read in a table.

    Lists and dicts change on most runs and would bury the field of interest,
    so only their size is shown.
    """
    if isinstance(value, list):
        return f"{len(value)} элем." if value else "пусто"
    if isinstance(value, dict):
        return f"{len(value)} пол." if value else "пусто"
    return value


def _between(before: dict, after: dict) -> list[tuple[str, tuple[Any, Any]]]:
    """What one run changed, field by field, in the shape the page renders."""
    names = (before.keys() | after.keys()) - BOOKKEEPING
    return sorted(
        (name, (_shown(before.get(name)), _shown(after.get(name))))
        for name in names if before.get(name) != after.get(name)
    )


def history(db: Database, label: str, node_id: str, limit: int = PAGE) -> list[dict]:
    """Every run that changed this record's row, newest first.

    The archive holds the state *before* each replacement, so the newest
    change is the gap between the last archived version and the live row.

    Args:
        label: Node label as the panel knows it; the prepared entity is
            looked up from it.
        limit: How many changes to show. The newest ones.

    Returns:
        One row per change: when, which run made it, the version it
        replaced, and the fields that moved.
    """
    entity = ENTITIES.get(label)
    if entity is None:
        return []
    archived = list(db[REVISIONS]
                    .find({"entity_type": entity, "entity_id": node_id})
                    .sort("version", -1).limit(limit))
    if not archived:
        return []
    archived.reverse()
    live = db[PreparedStore.COLLECTIONS[entity]].find_one({"_id": node_id})

    changes = []
    for index, row in enumerate(archived):
        # What replaced it: the next version filed, or the row as it stands.
        if index + 1 < len(archived):
            after = archived[index + 1]["snapshot"]
        elif live is not None:
            after = live
        else:
            # The row is gone: comparing with nothing would read as wiped fields.
            continue
        changes.append({
            "when": row.get("replaced_at", ""),
            "group": row.get("replaced_by_group", ""),
            "version": row.get("version"),
            "changes": _between(row.get("snapshot") or {}, after),
        })
    changes.reverse()
    return changes


def count(db: Database, label: str, node_id: str) -> int:
    """How many versions the archive holds for this record."""
    entity = ENTITIES.get(label)
    if entity is None:
        return 0
    return db[REVISIONS].count_documents({"entity_type": entity, "entity_id": node_id})
