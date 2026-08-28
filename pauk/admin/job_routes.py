"""Scheduled runs as a page: what is under way, what has been, and starting one.

Nothing here performs work. A form writes a job document and a separate
process picks it up, so no request waits on a publish and restarting the
panel cannot cut one in half.

Every value a form can carry is checked against something closed: the kind
against `JobKind`, the group against the groups that actually have prepared
rows, the dates against the selectors the CLI already uses. A command line
is never assembled — the worker looks its work up in a table by enum.
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
        "actors": sorted(db[store.COLLECTION].distinct("actor")),
        # Only groups that have prepared rows: publishing one that does not
        # exist loads nothing and looks like a broken publish.
        "groups": PreparedStore.known_groups(db),
        "today": date.today().isoformat()})


def _collect_payload(form) -> dict:
    """One collection run, from a work id or a date range.

    The group is derived, not typed: `group_name` is what `pauk run` uses,
    and a group invented here would be a second naming rule to keep in step
    with the first.
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
        # Told apart on purpose: PeriodSelector raises ValueError both for
        # a value that is not a date and for a range the wrong way round,
        # and one message for the two would be wrong half the time. The
        # browser's date box cannot send either, but a request that never
        # met the form can.
        for what, value in (("начало", date_from), ("конец", date_to)):
            try:
                date.fromisoformat(value)
            except ValueError:
                raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                    f"{what} периода — не дата: {value!r}") from None
        try:
            # The ordering rule stays where `pauk run` reads it; only the
            # wording is the panel's, since the selector says it in terms
            # of command-line flags.
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
        # Checked against the groups that exist, not only against the shape
        # of a name. The form offers a list, and a request that arrives
        # without the form must meet the same list — publishing a group
        # with no rows takes the graph lock to load nothing.
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

    The graph write is not made here and neither is the decision: the job
    document is the whole of it, and the worker does the rest. So there is
    no ordering to get wrong — the run either was asked for or was not.
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
        # A group that does not exist, a seed that is not a number: the
        # payload models refuse it before anything is stored.
        first = error.errors()[0]
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"{'.'.join(str(part) for part in first['loc'])}: "
                            f"{first['msg']}") from None
    logger.info("%s queued a %s job: %s", user.actor, kind, job.id)
    return RedirectResponse(f"/jobs?queued={job.id}", status_code=status.HTTP_303_SEE_OTHER)
