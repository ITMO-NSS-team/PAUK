"""Optional submission store adapter isolating MCP from graph/admin mutations."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Literal, Protocol

from pymongo.database import Database
from pymongo.errors import PyMongoError

from pauk.mcp.contracts import MCPError, ProposedRelation
from pauk.storage import link_suggestions

logger = logging.getLogger(__name__)

SubmissionStatus = Literal["queued", "already_queued", "already_reviewed"]


class SuggestionStore(Protocol):
    """Minimal async interface for recording agent-submitted link evidence."""

    async def submit(
        self,
        *,
        publication_id: str,
        proposed_relation: ProposedRelation,
        rationale: str,
        repository_id: str | None = None,
        repository_url: str | None = None,
        quote: str | None = None,
        evidence_url: str | None = None,
    ) -> tuple[str, SubmissionStatus]:
        """Store or deduplicate a submission and return (suggestion_id, status)."""
        ...


class MongoSuggestionStore:
    """Persist opt-in MCP submissions to an isolated MongoDB collection."""

    def __init__(
        self,
        db: Database,
        *,
        max_pending: int = link_suggestions.DEFAULT_MAX_PENDING,
        max_per_publication: int = link_suggestions.DEFAULT_MAX_PENDING_PER_PUBLICATION,
    ) -> None:
        self.db = db
        self.max_pending = max_pending
        self.max_per_publication = max_per_publication
        self._indexed = False
        self._index_lock = threading.Lock()

    def _submit_sync(
        self,
        *,
        publication_id: str,
        proposed_relation: ProposedRelation,
        rationale: str,
        repository_id: str | None = None,
        repository_url: str | None = None,
        quote: str | None = None,
        evidence_url: str | None = None,
    ) -> dict:
        if not self._indexed:
            with self._index_lock:
                if not self._indexed:
                    try:
                        link_suggestions.ensure_indexes(self.db)
                    except PyMongoError as exc:
                        logger.warning("suggestion index initialization failed: %s", exc)
                        raise
                    self._indexed = True
        return link_suggestions.submit(
            self.db,
            publication_id=publication_id,
            proposed_relation=proposed_relation,
            rationale=rationale,
            repository_id=repository_id,
            repository_url=repository_url,
            quote=quote,
            evidence_url=evidence_url,
            submitted_by="mcp",
            max_pending=self.max_pending,
            max_per_publication=self.max_per_publication,
        )

    async def submit(
        self,
        *,
        publication_id: str,
        proposed_relation: ProposedRelation,
        rationale: str,
        repository_id: str | None = None,
        repository_url: str | None = None,
        quote: str | None = None,
        evidence_url: str | None = None,
    ) -> tuple[str, SubmissionStatus]:
        """Run synchronous PyMongo submission logic off the event loop and sanitize errors."""
        try:
            result = await asyncio.to_thread(
                self._submit_sync,
                publication_id=publication_id,
                proposed_relation=proposed_relation,
                rationale=rationale,
                repository_id=repository_id,
                repository_url=repository_url,
                quote=quote,
                evidence_url=evidence_url,
            )
        except link_suggestions.SuggestionQueueFull as exc:
            raise MCPError("QUEUE_FULL") from exc
        except link_suggestions.SuggestionError as exc:
            raise MCPError("INPUT") from exc
        except PyMongoError as exc:
            raise MCPError("BACKEND") from exc
        return str(result["suggestion_id"]), result["status"]
