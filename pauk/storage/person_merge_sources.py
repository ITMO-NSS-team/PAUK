"""Durable pre-merge person rows, separate from the trimmable revision history."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from hashlib import sha256

from bson import BSON
from pymongo.database import Database

COLLECTION = "person_merge_sources"


def preserve(db: Database, ids: Iterable[str]) -> None:
    """Archive all live participants before any merge writes or deletions."""
    ids = sorted(set(ids))
    if not ids:
        return
    rows = list(db.persons.find({"id": {"$in": ids}}))
    if {row["id"] for row in rows} != set(ids):
        raise ValueError("cannot preserve every person participating in the merge")
    for row in rows:
        key = sha256(BSON.encode(row)).hexdigest()
        db[COLLECTION].update_one(
            {"_id": key},
            {"$setOnInsert": {"person_id": row["id"], "snapshot": row, "saved_at": datetime.now(UTC).isoformat()}},
            upsert=True,
        )


def snapshots(db: Database, ids: Iterable[str]) -> dict[str, list[dict]]:
    """Newest pre-merge state first, including provenance for republishing."""
    result: dict[str, list[dict]] = {}
    for entry in db[COLLECTION].find({"person_id": {"$in": list(ids)}}).sort("saved_at", -1):
        result.setdefault(entry["person_id"], []).append(entry["snapshot"])
    return result
