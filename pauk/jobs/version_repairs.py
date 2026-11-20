"""Rediscover durable version obligations through the existing job queue."""

from __future__ import annotations

import logging

from pymongo.database import Database
from pymongo.errors import PyMongoError

from pauk.jobs import store
from pauk.jobs.models import JobKind, VersionRepairRef, VersionsPayload
from pauk.storage import version_repair

logger = logging.getLogger(__name__)

RETRY_SECONDS = 60


def defer_attempt(db: Database, repair: VersionRepairRef, *, error: str | None = None) -> bool:
    """Leave a failed or interrupted obligation pending, without a tight retry loop."""
    try:
        return version_repair.defer(
            db, repair.collection_name, repair.document_id, repair.token,
            error=error, delay_seconds=RETRY_SECONDS,
        )
    except PyMongoError as failure:
        logger.warning("could not postpone version repair %s/%s: %s",
                       repair.collection_name, repair.document_id, failure)
        return False


def dispatch_pending(db: Database) -> int:
    """Schedule due generations, never making a job the durable source of truth.

    Defer before enqueue so a lost insert reply, cancellation, or worker crash
    cannot turn polling into an immediate retry loop. The intent stays on its
    decision document until the graph runner acknowledges that exact token.
    """
    dispatched = 0
    try:
        for collection_name, document_id, intent in version_repair.pending(db, due_only=True):
            repair = None
            try:
                repair = VersionRepairRef(collection_name=collection_name,
                                          document_id=document_id, token=intent["token"])
                payload = VersionsPayload(repair=repair, mode=intent.get("mode", "current"))
                if store.has_live_version_repair(db, repair):
                    defer_attempt(db, repair)
                    continue
                if not defer_attempt(db, repair):
                    continue
                store.enqueue(db, JobKind.VERSIONS, payload.model_dump(mode="json"),
                              actor="version-repair")
                dispatched += 1
            except Exception as error:
                logger.exception("could not dispatch version repair %s/%s", collection_name, document_id)
                if repair is not None:
                    defer_attempt(db, repair, error=f"{type(error).__name__}: {error}")
    except PyMongoError as error:
        logger.warning("could not discover pending version repairs: %s", error)
    return dispatched
