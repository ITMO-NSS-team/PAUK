"""The graph's health page.

Reads the answer a run saved (see `pauk.admin.health`); only the page for one
check queries the graph, with a `LIMIT`.
"""

from __future__ import annotations

import csv
import io
import logging

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, StreamingResponse

from pauk.admin import health
from pauk.admin.checks import BY_ID
from pauk.admin.deps import CurrentUser, Db, Graph, Session, templates
from pauk.admin.graph_stats import EXAMPLES_LIMIT_DEFAULT

logger = logging.getLogger("pauk.admin")

router = APIRouter()


@router.get("/health", response_class=HTMLResponse)
def overview(request: Request, user: CurrentUser, session: Session, db: Db):
    """What the last run of the checks found, readable by anyone who can sign in."""
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
    """The rows behind one check, queried from the graph now rather than read from the saved answer."""
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
    """The rows behind one check as a CSV file."""
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
        # Excel reads a CSV as the system encoding unless the file says otherwise.
        iter(["﻿" + buffer.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{check_id}.csv"'})
