from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from pymongo.database import Database


class RawStore:
    """Append-only storage for complete external-source responses.

    Every fetch is a document in the shared `raw` collection, tagged with
    `source` and `group`. Nothing is ever deduplicated or overwritten here -
    read() gives back this store's group's history for one source, oldest
    first.
    """

    def __init__(self, db: Database, group: str) -> None:
        self.db = db
        self.group = group

    def append(self, source: str, payload: dict[str, Any], request: dict[str, Any]) -> None:
        self.db.raw.insert_one({
            "source": source,
            "group": self.group,
            "fetched_at": datetime.now(UTC).isoformat(),
            "request": request,
            "payload": payload,
        })

    def read(self, source: str) -> Iterator[dict[str, Any]]:
        cursor = self.db.raw.find(
            {"source": source, "group": self.group},
            {"_id": False},
        ).sort("fetched_at", 1)
        yield from cursor
