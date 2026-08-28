"""Scheduled runs as a page: what is under way, what has been, starting one.

Nothing here performs work. A form writes a job document and a separate
process picks it up, so no request waits on a publish.

Every value a form can carry is checked against a closed set: the kind
against `JobKind`, the group against the groups that have prepared rows,
the dates against the selectors the CLI uses. No command line is assembled.
"""

from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from pauk.admin.deps import Admin, CsrfChecked, CurrentUser, Db, Session, templates
from pauk.jobs import store
from pauk.jobs.models import FINAL, JobKind, JobState
from pauk.pipeline.selectors import PeriodSelector
from pauk.storage import PreparedStore
from pauk.storage.naming import group_name, validate_group

logger = logging.getLogger("pauk.admin")

router = APIRouter()

# What each kind and state is called on the page. `JobKind.PUBLISH` is a
# name for the code, not for a reader.
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
        # Sorted so two renders list the counts the same way.
        "result": sorted((job.result or {}).items()),
        "payload": sorted((job.payload or {}).items()),
    }


@router.get("/jobs", response_class=HTMLResponse)
def jobs(request: Request, user: CurrentUser, session: Session, db: Db,
         kind: str = "", state: str = "", actor: str = "", page: int = 1):
    """The queue and the history of runs.

    Readable by anyone who can sign in, viewers included. Whether a publish
    is under way explains what somebody is looking at.
    """
    page = max(page, 1)
    filters = {"kind": kind, "state": state, "actor": actor}
    total = store.count(db, **filters)
    return templates.TemplateResponse(request, "jobs.html", {
        "user": user, "csrf": session["csrf"],
        # Apart from the history and above it. A run under way is what the
        # page is opened for, and it need not be the newest row.
        "under_way": [_shown(job) for job in store.running(db)],
        "rows": [_shown(job) for job in
                 store.recent(db, **filters, skip=(page - 1) * store.PAGE)],
        "total": total, "page": page,
        "pages": max((total + store.PAGE - 1) // store.PAGE, 1),
        "filters": filters, "kinds": KINDS, "states": STATES,
        "final": {str(name) for name in FINAL},
        "actors": sorted(db[store.COLLECTION].distinct("actor")),
        # Only groups with prepared rows. Publishing an empty one loads
        # nothing and looks like a broken publish.
        "groups": PreparedStore.known_groups(db),
        "today": date.today().isoformat()})


def _collect_payload(form) -> dict:
    """One collection run, from a work id or a date range.

    The group is derived with `group_name`, the same as `pauk run`. A name
    invented here would be a second rule to keep in step with the first.
    """
    work_id = str(form.get("work_id", "")).strip()
    date_from = str(form.get("date_from", "")).strip()
    date_to = str(form.get("date_to", "")).strip()
    if work_id and (date_from or date_to):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "выберите либо одну работу, либо период — не оба сразу")
    if not work_id and not (date_from and date_to):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "укажите работу или обе даты периода")
    if date_from:
        # PeriodSelector raises ValueError both for a value that is not a
        # date and for a range the wrong way round, and one message for the
        # two would be wrong half the time.
        for what, value in (("начало", date_from), ("конец", date_to)):
            try:
                date.fromisoformat(value)
            except ValueError:
                raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                    f"{what} периода — не дата: {value!r}") from None
        try:
            # The ordering rule stays where `pauk run` reads it. Only the
            # wording is the panel's, since the selector talks about flags.
            PeriodSelector(date_from, date_to)
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "начало периода позже его конца") from None
    try:
        group = validate_group(group_name(work_id=work_id or None,
                                          date_from=date_from or None,
                                          date_to=date_to or None))
    except ValueError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from None
    return {"group": group, "work_id": work_id or None,
            "date_from": date_from or None, "date_to": date_to or None}


def _payload_from(kind: JobKind, db, form) -> dict:
    """What the form said, in the shape this kind of job expects."""
    if kind is JobKind.COLLECT:
        return _collect_payload(form)
    if kind is JobKind.PUBLISH:
        group = str(form.get("group", "")).strip()
        # Checked against the groups that exist, not only the shape of a
        # name: a request arriving without the form meets the same list.
        if group not in PreparedStore.known_groups(db):
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                f"нет подготовленных строк для группы {group!r}")
        return {"group": group}
    if kind is JobKind.MAP:
        return {"public": bool(form.get("public")),
                "seed": int(str(form.get("seed", "")).strip() or 42)}
    return {}


@router.post("/jobs")
async def schedule(request: Request, user: Admin, db: Db, _: CsrfChecked):
    """Put a run in the queue.

    The job document is the whole of it and the worker does the rest, so
    there is no ordering to get wrong here.
    """
    form = await request.form()
    try:
        kind = JobKind(str(form.get("kind", "")))
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"неизвестный вид задачи: {form.get('kind')!r}") from None
    try:
        job = store.enqueue(db, kind, _payload_from(kind, db, form), actor=user.actor)
    except ValidationError as error:
        # The payload models refuse it before anything is stored.
        first = error.errors()[0]
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"{'.'.join(str(part) for part in first['loc'])}: "
                            f"{first['msg']}") from None
    logger.info("%s queued a %s job: %s", user.actor, kind, job.id)
    return RedirectResponse(f"/jobs?queued={job.id}", status_code=status.HTTP_303_SEE_OTHER)
