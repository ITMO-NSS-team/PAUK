"""Scheduled runs as a page: what is under way, and what has been.

Reading only. Starting a run is a separate step, and until it exists the
queue is filled from the terminal — the point of this page is that a person
looking at a stale field can tell whether a publish is rewriting it right
now, instead of guessing.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from pauk.admin.deps import CurrentUser, Db, Session, templates
from pauk.jobs import store
from pauk.jobs.models import FINAL, JobKind, JobState

router = APIRouter()

# What each kind and state is called on the page. `JobKind.PUBLISH` is a
# name for the code; a person reading the queue wants the words they would
# use themselves.
KINDS = {
    JobKind.COLLECT: "сбор",
    JobKind.PUBLISH: "публикация",
    JobKind.DEDUP: "дедуп",
    JobKind.MAP: "пересборка карты",
}

STATES = {
    JobState.QUEUED: "в очереди",
    JobState.CLAIMED: "принята",
    JobState.RUNNING: "идёт",
    JobState.DONE: "готово",
    JobState.FAILED: "ошибка",
    JobState.CANCELLED: "отменена",
}


def _shown(job) -> dict:
    """One job as the page reads it, without touching the stored document."""
    return {
        "id": job.id,
        "kind": KINDS.get(job.kind, str(job.kind)),
        "state": str(job.state),
        "state_ru": STATES.get(job.state, str(job.state)),
        "actor": job.actor,
        "worker": job.worker,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "heartbeat_at": job.heartbeat_at,
        "resource": job.resource,
        "error": job.error,
        "cancel_requested": job.cancel_requested,
        # Sorted so two renders of the same job list the counts in the same
        # order; a dict from Mongo carries whatever order it was written in.
        "result": sorted((job.result or {}).items()),
        "payload": sorted((job.payload or {}).items()),
    }


@router.get("/jobs", response_class=HTMLResponse)
def jobs(request: Request, user: CurrentUser, session: Session, db: Db,
         kind: str = "", state: str = "", actor: str = "", page: int = 1):
    """The queue and the history of runs.

    Readable by anyone who can sign in, viewers included: whether a publish
    is under way explains what somebody is looking at, and that is not a
    privilege.
    """
    page = max(page, 1)
    filters = {"kind": kind, "state": state, "actor": actor}
    total = store.count(db, **filters)
    return templates.TemplateResponse(request, "jobs.html", {
        "user": user, "csrf": session["csrf"],
        # Shown apart from the history, and above it: a run under way is
        # the thing the page is opened for, and it need not be the newest
        # row once a later job has already finished.
        "under_way": [_shown(job) for job in store.running(db)],
        "rows": [_shown(job) for job in
                 store.recent(db, **filters, skip=(page - 1) * store.PAGE)],
        "total": total, "page": page,
        "pages": max((total + store.PAGE - 1) // store.PAGE, 1),
        "filters": filters, "kinds": KINDS, "states": STATES,
        "final": {str(name) for name in FINAL},
        "actors": sorted(db[store.COLLECTION].distinct("actor"))})
