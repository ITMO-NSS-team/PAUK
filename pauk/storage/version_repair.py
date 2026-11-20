"""Durable version-reconciliation obligations stored with their source decision."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from pymongo.database import Database

FIELD = "version_repair"
COLLECTIONS = ("graph_overrides", "review_pairs")


def _now() -> datetime:
    moment = datetime.now(UTC)
    return moment.replace(microsecond=moment.microsecond // 1000 * 1000)


def make_intent(members: Sequence[str], *, mode: str = "current") -> dict:
    """Include this value in the same atomic update as the human decision."""
    if len(members) != 2 or any(not isinstance(member, str) or not member for member in members):
        raise ValueError("a version repair needs two distinct publication IDs")
    pair = sorted(set(members))
    if len(pair) != 2:
        raise ValueError("a version repair needs two distinct publication IDs")
    if mode not in ("current", "reopen"):
        raise ValueError("unknown version reconciliation mode")
    moment = _now()
    return {"token": uuid4().hex, "members": pair, "mode": mode, "requested_at": moment, "next_attempt_at": moment}


def _collection(db: Database, name: str):
    if name not in COLLECTIONS:
        raise ValueError("unknown version-repair source")
    return db[name]


def pending(db: Database, *, due_only: bool = False) -> Iterator[tuple[str, str, dict]]:
    """Rediscover obligations independently of whether a job was ever inserted."""
    query: dict = {FIELD: {"$exists": True}}
    if due_only:
        query["$or"] = [
            {f"{FIELD}.next_attempt_at": {"$lte": _now()}},
            {f"{FIELD}.next_attempt_at": {"$exists": False}},
        ]
    for name in COLLECTIONS:
        for row in _collection(db, name).find(query):
            intent = row.get(FIELD)
            if isinstance(intent, dict) and intent.get("token") and len(intent.get("members", [])) == 2:
                yield name, row["_id"], intent


def load(db: Database, collection_name: str, document_id: str, token: str) -> dict | None:
    """An older attempt must not apply or acknowledge a newer decision's intent."""
    row = _collection(db, collection_name).find_one({"_id": document_id, f"{FIELD}.token": token})
    return row[FIELD] if row else None


def ack(db: Database, collection_name: str, document_id: str, token: str) -> bool:
    """Acknowledge only after both graph and questions have been synchronized."""
    result = _collection(db, collection_name).update_one(
        {"_id": document_id, f"{FIELD}.token": token},
        {"$unset": {FIELD: ""}, "$set": {"version_repair_completed": {"token": token, "at": _now()}}},
    )
    return result.modified_count > 0


def defer(
    db: Database,
    collection_name: str,
    document_id: str,
    token: str,
    *,
    error: str | None = None,
    delay_seconds: int = 60,
) -> bool:
    """Back off an attempt without discarding the source obligation."""
    values: dict = {f"{FIELD}.next_attempt_at": _now() + timedelta(seconds=max(1, delay_seconds))}
    update: dict = {"$set": values}
    if error is not None:
        values[f"{FIELD}.last_error"] = str(error)[:2000]
        update["$inc"] = {f"{FIELD}.failures": 1}
    result = _collection(db, collection_name).update_one(
        {"_id": document_id, f"{FIELD}.token": token},
        update,
    )
    return result.matched_count > 0
