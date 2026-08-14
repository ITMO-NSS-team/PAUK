from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pymongo.database import Database


class LlmLogStore:
    """Full request/response log for one LLM use case - one collection per
    call site (e.g. "llm_logs_link_relevance"), not a shared collection,
    so each use case's logs can be indexed/retained independently."""

    def __init__(self, db: Database, collection: str) -> None:
        self.collection = db[collection]

    def record(
        self, *, group: str, model: str, prompt: str,
        raw_response: dict[str, Any] | None, parsed: dict[str, Any] | None,
        usage: dict[str, Any] | None, error: str | None, context: dict[str, Any],
    ) -> None:
        self.collection.insert_one({
            "group": group,
            "model": model,
            "prompt": prompt,
            "raw_response": raw_response,
            "parsed": parsed,
            "usage": usage,
            "error": error,
            "context": context,
            "called_at": datetime.now(UTC).isoformat(),
        })
