"""The graph's health, as a page.

The same thirty-two checks the map's tab runs, read from the answer a run
wrote down (see `pauk.admin.health`). The page itself asks the graph
nothing — except when somebody opens one check to see the rows behind it,
which is a `LIMIT`-ed query and worth running fresh.
"""

from __future__ import annotations

import csv
import io
import logging

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, StreamingResponse

from pauk.admin import health
from pauk.admin.deps import CurrentUser, Db, Graph, Session, templates
from pauk.gui.checks import BY_ID
from pauk.gui.generate_stats import EXAMPLES_LIMIT_DEFAULT

logger = logging.getLogger("pauk.admin")

router = APIRouter()


@router.get("/health", response_class=HTMLResponse)
def overview(request: Request, user: CurrentUser, session: Session, db: Db):
    """What the last run of the checks found.

    Readable by anyone who can sign in: it says what the data looks like
    and changes nothing.
    """
    saved = health.latest(db)
    checks = (saved or {}).get("stats", {}).get("checks") or []
    return templates.TemplateResponse(request, "health.html", {
        "user": user, "csrf": session["csrf"],
        "computed_at": (saved or {}).get("computed_at"),
        "totals": (saved or {}).get("stats", {}).get("totals") or {},
        "groups": health.grouped(checks),
        "verdict": health.verdict(checks),
        "words": health.WORDS,
        "openable": health.openable,
    })


@router.get("/health/{check_id}", response_class=HTMLResponse)
def behind(request: Request, check_id: str, user: CurrentUser, session: Session,
           db: Db, graph: Graph, limit: int = EXAMPLES_LIMIT_DEFAULT):
    """The rows behind one check, asked of the graph right now.

    Not read from the saved answer: a list of records that were wrong last
    Tuesday is the wrong kind of wrong, and the query is capped anyway.
    """
    if check_id not in BY_ID:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "такой проверки нет")
    try:
        found = health.rows_behind(graph, check_id, limit)
    except ValueError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from None
    return templates.TemplateResponse(request, "health_check.html", {
        "user": user, "csrf": session["csrf"], "check": found,
        "computed_at": (health.latest(db) or {}).get("computed_at"),
    })


@router.get("/health/{check_id}/csv")
def as_csv(check_id: str, user: CurrentUser, graph: Graph,
           limit: int = EXAMPLES_LIMIT_DEFAULT):
    """The same rows as a file, for the person who has to go and fix them.

    A list of a few hundred records is worked through in a spreadsheet, not
    in a browser tab.
    """
    if check_id not in BY_ID:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "такой проверки нет")
    try:
        found = health.rows_behind(graph, check_id, limit)
    except ValueError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from None
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(found["columns"])
    writer.writerows(found["rows"])
    logger.info("%s exported %s (%d row(s))", user.actor, check_id, len(found["rows"]))
    return StreamingResponse(
        # Excel reads a CSV as the system encoding unless the file says
        # otherwise, and these carry Russian names.
        iter(["﻿" + buffer.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{check_id}.csv"'})
