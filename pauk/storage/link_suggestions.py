"""Agent-submitted link evidence awaiting human moderation.

CoScientist (via opt-in MCP) records evidence for code repositories or
full-text PDFs in MongoDB (`link_suggestions`). Nothing reaches Neo4j or
`graph_overrides` until an editor reviews and approves the row in the admin
panel (`/suggestions`).

Storage stays independent of `pauk/mcp/` and `pauk/graph/` so neither MCP nor
pipeline imports leak across layers.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

logger = logging.getLogger(__name__)

COLLECTION = "link_suggestions"

PENDING = "pending"
APPLYING = "applying"
APPROVED = "approved"
REJECTED = "rejected"
STATUSES = (PENDING, APPLYING, APPROVED, REJECTED)

IMPLEMENTS = "implements"
NOT_IMPLEMENTS = "not_implements"
MENTIONS = "mentions"
FULL_TEXT = "full_text"
RELATIONS = (IMPLEMENTS, NOT_IMPLEMENTS, MENTIONS, FULL_TEXT)

DEFAULT_MAX_PENDING = 500
DEFAULT_MAX_PENDING_PER_PUBLICATION = 10
APPLYING_LEASE_SECONDS = 300


class SuggestionError(Exception):
    """A malformed or invalid link suggestion."""


class SuggestionQueueFull(SuggestionError):
    """Queue capacity reached for pending suggestions."""


def _now() -> datetime:
    """Current UTC time truncated to BSON millisecond precision."""
    moment = datetime.now(UTC)
    return moment.replace(microsecond=moment.microsecond // 1000 * 1000)


def dedup_key(
    publication_id: str,
    proposed_relation: str,
    repository_id: str | None = None,
    repository_url: str | None = None,
    evidence_url: str | None = None,
) -> str:
    """Deterministic key identifying one (publication, relation, target) claim."""
    if not publication_id:
        raise SuggestionError("publication_id is required")
    if proposed_relation not in RELATIONS:
        raise SuggestionError(f"unknown proposed_relation: {proposed_relation!r}")
    if proposed_relation == FULL_TEXT:
        if not evidence_url:
            raise SuggestionError("evidence_url is required for full_text suggestions")
        target_key = evidence_url
    else:
        if repository_url:
            target_key = repository_url
        elif repository_id:
            target_key = f"id:{repository_id}"
        else:
            raise SuggestionError("repository_id or repository_url is required")
    payload = json.dumps([publication_id, proposed_relation, target_key], ensure_ascii=False)
    return sha256(payload.encode("utf-8")).hexdigest()


def content_hash(
    key: str,
    rationale: str,
    quote: str | None = None,
    evidence_url: str | None = None,
) -> str:
    """Digest of a claim together with its submitted evidence payload."""
    payload = json.dumps([key, rationale, quote or "", evidence_url or ""], ensure_ascii=False)
    return sha256(payload.encode("utf-8")).hexdigest()


def ensure_indexes(db: Database) -> None:
    """Create indexes used by MCP submission dedup and admin moderation queues."""
    collection = db[COLLECTION]
    collection.create_index([("status", 1), ("submitted_at", 1)])
    collection.create_index([("status", 1), ("reviewed_at", -1)])
    collection.create_index([("dedup_key", 1), ("status", 1)])
    collection.create_index([("publication_id", 1), ("status", 1)])
    collection.create_index([("content_hash", 1)])
    collection.create_index(
        [("dedup_key", 1)],
        unique=True,
        name="uniq_active_dedup_key",
        partialFilterExpression={"active": True},
    )


def _bump_active(collection, key: str, moment: datetime) -> dict[str, Any] | None:
    return collection.find_one_and_update(
        {"dedup_key": key, "status": {"$in": [PENDING, APPLYING]}},
        {"$inc": {"submission_count": 1}, "$set": {"last_submitted_at": moment, "active": True}},
        return_document=ReturnDocument.AFTER,
    )


def submit(
    db: Database,
    *,
    publication_id: str,
    proposed_relation: str,
    rationale: str,
    repository_id: str | None = None,
    repository_url: str | None = None,
    quote: str | None = None,
    evidence_url: str | None = None,
    submitted_by: str = "mcp",
    max_pending: int = DEFAULT_MAX_PENDING,
    max_per_publication: int = DEFAULT_MAX_PENDING_PER_PUBLICATION,
) -> dict[str, Any]:
    """Queue a link suggestion or deduplicate against active/identical submissions."""
    if not rationale or not rationale.strip():
        raise SuggestionError("rationale is required")
    key = dedup_key(
        publication_id,
        proposed_relation,
        repository_id=repository_id,
        repository_url=repository_url,
        evidence_url=evidence_url,
    )
    digest = content_hash(key, rationale, quote=quote, evidence_url=evidence_url)
    moment = _now()
    collection = db[COLLECTION]

    existing = _bump_active(collection, key, moment)
    if existing is not None:
        return {
            "suggestion_id": existing["_id"],
            "status": "already_queued",
            "document": existing,
        }

    reviewed = collection.find_one(
        {"content_hash": digest, "status": {"$in": [APPROVED, REJECTED]}},
        sort=[("reviewed_at", -1)],
    )
    if reviewed is not None:
        return {
            "suggestion_id": reviewed["_id"],
            "status": "already_reviewed",
            "document": reviewed,
        }

    if collection.count_documents({"status": {"$in": [PENDING, APPLYING]}}) >= max_pending:
        raise SuggestionQueueFull("pending link suggestions queue is full")
    if (
        collection.count_documents(
            {"publication_id": publication_id, "status": {"$in": [PENDING, APPLYING]}}
        )
        >= max_per_publication
    ):
        raise SuggestionQueueFull("too many pending suggestions for this publication")

    doc = {
        "_id": f"sug_{uuid4().hex[:24]}",
        "dedup_key": key,
        "content_hash": digest,
        "status": PENDING,
        "active": True,
        "publication_id": publication_id,
        "proposed_relation": proposed_relation,
        "repository_id": repository_id,
        "repository_url": repository_url,
        "quote": quote,
        "rationale": rationale,
        "evidence_url": evidence_url,
        "submitted_by": submitted_by,
        "submitted_at": moment,
        "last_submitted_at": moment,
        "submission_count": 1,
    }
    try:
        collection.insert_one(doc)
    except DuplicateKeyError:
        existing = _bump_active(collection, key, moment)
        if existing is not None:
            return {
                "suggestion_id": existing["_id"],
                "status": "already_queued",
                "document": existing,
            }
        raise
    logger.info("link_suggestions: queued %s (%s for %s)", doc["_id"], proposed_relation, publication_id)
    return {
        "suggestion_id": doc["_id"],
        "status": "queued",
        "document": doc,
    }


def get(db: Database, suggestion_id: str) -> dict[str, Any] | None:
    """Fetch one suggestion document by id, or None."""
    return db[COLLECTION].find_one({"_id": suggestion_id})


def _status_filter(status: str) -> tuple[dict[str, Any], list[tuple[str, int]]]:
    if status == PENDING:
        return {"status": {"$in": [PENDING, APPLYING]}}, [("submitted_at", 1), ("_id", 1)]
    if status in (APPROVED, REJECTED):
        return {"status": status}, [("reviewed_at", -1), ("_id", -1)]
    raise SuggestionError(f"unknown status tab: {status!r}")


def count(db: Database, *, status: str = PENDING) -> int:
    """Count suggestions in a moderation tab (`pending` includes `applying` leases)."""
    query, _ = _status_filter(status)
    return db[COLLECTION].count_documents(query)


def list_suggestions(
    db: Database,
    *,
    status: str = PENDING,
    limit: int = 50,
    skip: int = 0,
) -> list[dict[str, Any]]:
    """Return bounded, deterministically ordered suggestions for one moderation tab."""
    query, order = _status_filter(status)
    return list(db[COLLECTION].find(query).sort(order).skip(skip).limit(limit))


def prior_decisions(db: Database, dedup_keys: list[str]) -> dict[str, dict[str, Any]]:
    """Latest human moderation decision for each dedup key, if already reviewed before."""
    keys = [key for key in set(dedup_keys) if key]
    if not keys:
        return {}
    decisions: dict[str, dict[str, Any]] = {}
    cursor = db[COLLECTION].find(
        {"dedup_key": {"$in": keys}, "status": {"$in": [APPROVED, REJECTED]}},
    ).sort([("reviewed_at", -1), ("_id", -1)])
    for row in cursor:
        decisions.setdefault(row["dedup_key"], row)
    return decisions


def _as_utc(moment: datetime) -> datetime:
    """Normalize naive datetimes (as returned by mongomock) to UTC."""
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def is_recoverable(row: dict[str, Any], *, moment: datetime | None = None) -> bool:
    """True when an `applying` suggestion is flagged for recovery or its lease expired."""
    if row.get("status") != APPLYING:
        return False
    if row.get("needs_recovery"):
        return True
    applying_at = row.get("applying_at")
    if not isinstance(applying_at, datetime):
        return False
    now = _as_utc(moment or _now())
    return _as_utc(applying_at) < now - timedelta(seconds=APPLYING_LEASE_SECONDS)


def claim_for_apply(db: Database, suggestion_id: str, actor: str) -> dict[str, Any] | None:
    """Atomically claim a pending suggestion; stuck `applying` rows use recovery."""
    moment = _now()
    token = uuid4().hex
    return db[COLLECTION].find_one_and_update(
        {"_id": suggestion_id, "status": PENDING},
        {
            "$set": {
                "status": APPLYING,
                "active": True,
                "applying_by": actor,
                "applying_at": moment,
                "apply_token": token,
            },
            "$unset": {"needs_recovery": "", "recovery_reason": ""},
        },
        return_document=ReturnDocument.AFTER,
    )


def mark_needs_recovery(
    db: Database,
    suggestion_id: str,
    apply_token: str,
    *,
    reason: str = "",
) -> bool:
    """Flag an `applying` suggestion for immediate recovery when graph state is uncertain."""
    result = db[COLLECTION].update_one(
        {"_id": suggestion_id, "status": APPLYING, "apply_token": apply_token},
        {
            "$set": {
                "active": True,
                "needs_recovery": True,
                "recovery_reason": reason,
            }
        },
    )
    return result.matched_count > 0


def claim_for_recovery(db: Database, suggestion_id: str, actor: str) -> dict[str, Any] | None:
    """Atomically reclaim a stuck `applying` suggestion after lease expiry or uncertainty."""
    moment = _now()
    stale_before = moment - timedelta(seconds=APPLYING_LEASE_SECONDS)
    token = uuid4().hex
    return db[COLLECTION].find_one_and_update(
        {
            "_id": suggestion_id,
            "status": APPLYING,
            "$or": [
                {"needs_recovery": True},
                {"applying_at": {"$lt": stale_before}},
            ],
        },
        {
            "$set": {
                "active": True,
                "applying_by": actor,
                "applying_at": moment,
                "apply_token": token,
            },
            "$unset": {"needs_recovery": "", "recovery_reason": ""},
        },
        return_document=ReturnDocument.AFTER,
    )


def release_claim(db: Database, suggestion_id: str, apply_token: str) -> bool:
    """Return a claimed suggestion back to pending when validation or confirmed non-write aborts."""
    result = db[COLLECTION].update_one(
        {"_id": suggestion_id, "status": APPLYING, "apply_token": apply_token},
        {
            "$set": {"status": PENDING, "active": True},
            "$unset": {
                "applying_by": "",
                "applying_at": "",
                "apply_token": "",
                "needs_recovery": "",
                "recovery_reason": "",
            },
        },
    )
    return result.matched_count > 0


def complete_approval(
    db: Database,
    suggestion_id: str,
    apply_token: str,
    *,
    actor: str,
    note: str = "",
    applied_effect: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Mark a claimed suggestion as approved after graph mutation and override persistence."""
    return db[COLLECTION].find_one_and_update(
        {"_id": suggestion_id, "status": APPLYING, "apply_token": apply_token},
        {
            "$set": {
                "status": APPROVED,
                "active": False,
                "reviewed_by": actor,
                "reviewed_at": _now(),
                "review_note": note,
                "applied_effect": dict(applied_effect or {}),
            },
            "$unset": {
                "applying_by": "",
                "applying_at": "",
                "apply_token": "",
                "needs_recovery": "",
                "recovery_reason": "",
            },
        },
        return_document=ReturnDocument.AFTER,
    )


def reject(
    db: Database,
    suggestion_id: str,
    *,
    actor: str,
    note: str = "",
) -> dict[str, Any] | None:
    """Reject a pending suggestion without touching Neo4j (`applying` rows must recover first)."""
    moment = _now()
    return db[COLLECTION].find_one_and_update(
        {"_id": suggestion_id, "status": PENDING},
        {
            "$set": {
                "status": REJECTED,
                "active": False,
                "reviewed_by": actor,
                "reviewed_at": moment,
                "review_note": note,
            },
            "$unset": {
                "applying_by": "",
                "applying_at": "",
                "apply_token": "",
                "needs_recovery": "",
                "recovery_reason": "",
            },
        },
        return_document=ReturnDocument.AFTER,
    )
