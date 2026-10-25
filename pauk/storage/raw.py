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

    def append(self, source: str, payload: dict[str, Any], request: dict[str, Any],
               *, provider: str | None = None, record_type: str = "publication",
               record_key: str | None = None, schema_version: int | None = None) -> None:
        row = {
            "source": source,
            "group": self.group,
            "fetched_at": datetime.now(UTC).isoformat(),
            "request": request,
            "payload": payload,
        }
        if provider is not None:
            row.update({
                "provider": provider,
                "record_type": record_type,
                "record_key": record_key,
                "schema_version": schema_version or 2,
            })
        self.db.raw.insert_one(row)

    def read(self, source: str) -> Iterator[dict[str, Any]]:
        cursor = self.db.raw.find(
            {"source": source, "group": self.group},
            {"_id": False},
        ).sort("fetched_at", 1)
        yield from cursor

    def read_provider(self, provider: str) -> Iterator[dict[str, Any]]:
        """Read v2 envelopes, plus the provider's v1 compatibility rows."""
        legacy_source = "openalex_works" if provider == "openalex" else f"{provider}_publications"
        cursor = self.db.raw.find(
            {"group": self.group, "$or": [
                {"provider": provider},
                {"provider": {"$exists": False}, "source": legacy_source},
            ]},
            {"_id": False},
        ).sort("fetched_at", 1)
        yield from cursor
