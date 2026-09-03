"""The queue: putting a job in, taking it out, saying how it ended.

One document per scheduled run. Nothing here performs work or touches
Neo4j, which is what lets the queue be tested without a graph.
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from pymongo.database import Database

from pauk.jobs.models import (
    FINAL,
    Job,
    JobKind,
    JobState,
    aware,
    now,
    parse_payload,
    resource_for,
)

logger = logging.getLogger(__name__)

COLLECTION = "jobs"

PAGE = 50

# How long a job may go without saying it is alive before it counts as
# abandoned. The beat is every minute (worker.BEAT_SECONDS), so this is
# several missed beats, not a slow step: the beat runs in its own thread
# and does not wait for the work.
STALE_MINUTES = 5


def enqueue(db: Database, kind: JobKind, payload: dict | None = None,
            actor: str = "unknown") -> Job:
    """Schedule a run.

    The payload is checked here, not when the worker picks it up, so a bad
    form is refused while somebody is still looking at the page.

    Raises:
        ValidationError: The payload does not describe this kind of job.
    """
    kind = JobKind(kind)
    parsed = parse_payload(kind, payload or {})
    moment = now()
    document = {
        "_id": secrets.token_urlsafe(12),
        "kind": str(kind),
        "payload": parsed.model_dump(mode="json"),
        "resource": resource_for(kind, parsed),
        "state": str(JobState.QUEUED),
        "actor": actor,
        "worker": None,
        "created_at": moment,
        "started_at": None,
        "finished_at": None,
        "heartbeat_at": None,
        "result": {},
        "error": None,
        "cancel_requested": False,
    }
    db[COLLECTION].insert_one(document)
    logger.info("job %s queued: %s by %s", document["_id"], kind, actor)
    return _as_job(document)


def claim(db: Database, worker: str) -> Job | None:
    """Take the oldest queued job, or None when there is nothing to do.

    One operation, so two workers cannot walk away with the same document.
    The resource is taken separately; a job whose resource is busy goes
    back with `requeue`.
    """
    moment = now()
    document = db[COLLECTION].find_one_and_update(
        {"state": str(JobState.QUEUED)},
        {"$set": {"state": str(JobState.CLAIMED), "worker": worker,
                  "heartbeat_at": moment}},
        # `_id` only breaks a tie. Two jobs queued inside one millisecond
        # share a created_at, and the order would otherwise be arbitrary.
        sort=[("created_at", 1), ("_id", 1)],
        return_document=True)
    if document is None:
        return None
    logger.info("job %s claimed by %s", document["_id"], worker)
    return _as_job(document)


def start(db: Database, job_id: str) -> bool:
    """Mark a claimed job as actually running, now that it holds its resource."""
    moment = now()
    result = db[COLLECTION].update_one(
        {"_id": job_id, "state": str(JobState.CLAIMED)},
        {"$set": {"state": str(JobState.RUNNING), "started_at": moment,
                  "heartbeat_at": moment}})
    return result.matched_count > 0


def requeue(db: Database, job_id: str) -> bool:
    """Put a job back because the resource it needs was taken.

    Not a failure: the run is still wanted, just not now. Accepts a running
    job as well as a claimed one, because a resource turns out to be busy
    only after the job has been marked as started.
    """
    result = db[COLLECTION].update_one(
        {"_id": job_id, "state": {"$in": [str(JobState.CLAIMED), str(JobState.RUNNING)]}},
        {"$set": {"state": str(JobState.QUEUED), "worker": None,
                  "started_at": None, "heartbeat_at": None}})
    return result.matched_count > 0


def heartbeat(db: Database, job_id: str) -> bool:
    """Say the run is still alive. A job that stops saying so is stuck.

    Answered by `matched_count`. Two beats inside one millisecond write the
    same value and Mongo reports nothing modified, but the question is
    whether a running job was found.
    """
    result = db[COLLECTION].update_one(
        {"_id": job_id, "state": str(JobState.RUNNING)},
        {"$set": {"heartbeat_at": now()}})
    return result.matched_count > 0


def finish(db: Database, job_id: str, result: dict[str, int] | None = None) -> bool:
    """Record a run that completed, with whatever counts it produced."""
    return _settle(db, job_id, JobState.DONE, result=dict(result or {}))


def fail(db: Database, job_id: str, error: str) -> bool:
    """Record a run that raised. The message is kept for the page to show."""
    return _settle(db, job_id, JobState.FAILED, error=error)


def cancelled(db: Database, job_id: str) -> bool:
    """Record a run that stopped because it was asked to."""
    return _settle(db, job_id, JobState.CANCELLED)


def request_cancel(db: Database, job_id: str) -> bool:
    """Ask a job to stop.

    A flag rather than a signal, so a half-written batch is never abandoned
    in the middle. A job that has not started yet is cancelled outright.
    """
    moment = now()
    queued = db[COLLECTION].update_one(
        {"_id": job_id, "state": str(JobState.QUEUED)},
        {"$set": {"state": str(JobState.CANCELLED), "cancel_requested": True,
                  "finished_at": moment}})
    if queued.matched_count:
        return True
    running = db[COLLECTION].update_one(
        {"_id": job_id, "state": {"$in": [str(JobState.CLAIMED), str(JobState.RUNNING)]}},
        {"$set": {"cancel_requested": True}})
    # Asking twice must not answer "no such job" the second time.
    return running.matched_count > 0


def _settle(db: Database, job_id: str, state: JobState, **fields) -> bool:
    """Move a job to a state it will not leave."""
    update = {"state": str(state), "finished_at": now(), **fields}
    result = db[COLLECTION].update_one(
        {"_id": job_id, "state": {"$nin": [str(name) for name in FINAL]}},
        {"$set": update})
    if result.matched_count:
        logger.info("job %s %s", job_id, state)
    return result.matched_count > 0


def is_stale(job: Job, minutes: int = STALE_MINUTES) -> bool:
    """Whether nobody is running this job any more.

    The beat comes from a thread of its own, independent of the work, so a
    job that stops beating is a job whose process is gone — killed worker,
    a machine that went away, Ctrl+C at the wrong moment.
    """
    if job.state not in (JobState.CLAIMED, JobState.RUNNING):
        return False
    last = job.heartbeat_at or job.started_at or job.created_at
    return aware(last) < now() - timedelta(minutes=minutes)


def reap_stale(db: Database, minutes: int = STALE_MINUTES) -> int:
    """Settle jobs whose worker stopped answering.

    Without this they sit in "under way" for ever: the only thing that ever
    moved a job out of that state was the worker that had it, and that
    worker is gone. A run somebody asked to stop is recorded as stopped;
    anything else as failed, because it was.

    Returns:
        How many were settled.
    """
    settled = 0
    for job in running(db):
        if not is_stale(job, minutes):
            continue
        if job.cancel_requested:
            settled += cancelled(db, job.id)
        else:
            settled += fail(db, job.id, "воркер перестал отвечать")
    if settled:
        logger.warning("settled %d job(s) nobody was running", settled)
    return settled


def read(db: Database, job_id: str) -> Job | None:
    document = db[COLLECTION].find_one({"_id": job_id})
    return _as_job(document) if document else None


def running(db: Database, resource: str | None = None) -> list[Job]:
    """Jobs under way, for the banner that warns an editor.

    Includes claimed ones. The work has not started yet, but it is about
    to, and somebody deciding whether to save now wants to know.
    """
    query: dict = {"state": {"$in": [str(JobState.CLAIMED), str(JobState.RUNNING)]}}
    if resource is not None:
        query["resource"] = resource
    rows = db[COLLECTION].find(query).sort([("created_at", 1), ("_id", 1)])
    return [_as_job(row) for row in rows]


def recent(db: Database, *, kind: str = "", state: str = "", actor: str = "",
           limit: int = PAGE, skip: int = 0) -> list[Job]:
    """One page of the history, newest first."""
    query = {name: value for name, value
             in (("kind", kind), ("state", state), ("actor", actor)) if value}
    rows = db[COLLECTION].find(query).sort(
        [("created_at", -1), ("_id", -1)]).skip(skip).limit(limit)
    return [_as_job(row) for row in rows]


def count(db: Database, *, kind: str = "", state: str = "", actor: str = "") -> int:
    query = {name: value for name, value
             in (("kind", kind), ("state", state), ("actor", actor)) if value}
    return db[COLLECTION].count_documents(query)


def _as_job(document: dict) -> Job:
    """One stored document as a Job.

    Validated rather than trusted, so a document written by an older
    version fails here instead of halfway through a run.
    """
    return Job.model_validate({**document, "id": document["_id"]})
