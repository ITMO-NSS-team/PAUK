"""Wiring shared by every route: the database, the graph, the caller.

The rule this module enforces is the one that matters most for the panel.
Neo4j is not reachable from outside the perimeter, so the panel is the
only way to write to the graph, and every write has to go through
`pauk.graph.mutations`. Routes therefore never receive a raw driver —
they receive the audited client, and the audit sink records who did what
while `actor_context` is held.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.templating import Jinja2Templates
from neo4j.exceptions import AuthError, ServiceUnavailable
from pymongo.database import Database
from pymongo.errors import PyMongoError

from pauk.admin.auth import COOKIE, User, check_csrf, read_session
from pauk.jobs import store
from pauk.jobs.models import GRAPH
from pauk.settings import Settings


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_config(request: Request) -> Settings:
    return request.app.state.config


def get_session(request: Request, db: Annotated[Database, Depends(get_db)]) -> dict | None:
    """The caller's session, or None. Never raises — used by the login page too."""
    return read_session(db, request.cookies.get(COOKIE))


def require_user(session: Annotated[dict | None, Depends(get_session)]) -> User:
    """The signed-in caller.

    Raises:
        HTTPException: 401 when there is no live session. The panel shows
            personal data and is the only door to the graph, so this is
            the default for every route except the login page itself.
    """
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "sign in first")
    return User(login=session["login"], role=session["role"])


def require_editor(user: Annotated[User, Depends(require_user)]) -> User:
    """A caller allowed to change the graph, as opposed to read it."""
    if not user.can_write:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this account can only read")
    return user


async def require_csrf(request: Request,
                       session: Annotated[dict | None, Depends(get_session)]) -> None:
    """Reject a form that did not come from our own page.

    The session cookie travels with a cross-site POST by itself, so it
    cannot prove where the request came from; a token the other site has
    no way to read can. Read the body through the request so the check
    runs before the route sees anything.
    """
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "sign in first")
    form = await request.form()
    if not check_csrf(session, form.get("csrf")):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "stale form, open the page again")


def graph_for(request: Request, user: Annotated[User, Depends(require_user)]) -> Iterator:
    """An audited graph client with the caller's name attached.

    The driver is shared by the whole application and the audited wrapper
    is per request. That became possible once the actor moved onto the
    wrapper: while it was read from a contextvar, a shared driver would
    have reported whoever was set last, so each request opened and tore
    down its own connection pool — twice on the overview, which counts
    nodes through a second client of its own.

    Raises:
        HTTPException: 503 when the graph cannot be reached — no password
            configured, or nothing listening. Both are setup problems, not
            programming errors, and answering them with a stack trace tells
            the person nothing about what to fix.
    """
    try:
        client = request.app.state.graph.audited(actor=user.actor, source="admin-ui")
    except ValueError as error:
        logger.warning("graph unavailable: %s", error)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(error)) from None
    except (ServiceUnavailable, AuthError) as error:
        logger.warning("graph unavailable: %s", error)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            f"cannot reach Neo4j at {request.app.state.config.neo4j_uri}") from None
    try:
        yield client
    finally:
        client.close()


# Named aliases so routes read as `db: Db` instead of repeating the
# Annotated form in every signature.
logger = logging.getLogger("pauk.admin")

def plural(count: int, one: str, few: str, many: str) -> str:
    """Russian noun agreement: 1 узел, 2 узла, 5 узлов.

    Counts are shown beside every label on the overview, and "1 полей"
    reads as a bug in the page rather than as a number.
    """
    if count % 10 == 1 and count % 100 != 11:
        return one
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return few
    return many


def moment(value) -> str:
    """A stored time as "2026-08-28 14:03:11", or "" when there is none.

    Job times are datetimes, unlike the feed's isoformat strings, and a
    bare str() of one carries microseconds and a timezone that say nothing
    to a reader.
    """
    if value is None:
        return ""
    return value.strftime("%Y-%m-%d %H:%M:%S")


def running_job(request: Request) -> dict:
    """The graph job under way, for the warning strip on every page.

    A context processor rather than an argument threaded through every
    route: the strip belongs to the layout, and a route that forgot to pass
    it would silently stop warning.

    Reading the queue must never be what takes the panel down, so a failure
    here leaves the strip off rather than the page.
    """
    db = getattr(request.app.state, "db", None)
    if db is None:
        return {"graph_job": None}
    try:
        under_way = store.running(db, resource=GRAPH)
    except PyMongoError as error:
        logger.warning("cannot read the queue for the banner: %s", error)
        return {"graph_job": None}
    return {"graph_job": under_way[0] if under_way else None}


templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"),
                            context_processors=[running_job])
templates.env.filters["plural"] = plural
templates.env.filters["moment"] = moment


def job_words(kind) -> str:
    """A job kind in the words the panel uses for it.

    Imported late: `job_routes` reads `templates` from here, so naming it
    at the top would close the circle.
    """
    from pauk.admin.job_routes import KINDS
    return KINDS.get(kind, str(kind))


templates.env.filters["job_words"] = job_words

Db = Annotated[Database, Depends(get_db)]
Config = Annotated[Settings, Depends(get_config)]
Session = Annotated[dict | None, Depends(get_session)]
CurrentUser = Annotated[User, Depends(require_user)]
Editor = Annotated[User, Depends(require_editor)]
CsrfChecked = Annotated[None, Depends(require_csrf)]
Graph = Annotated[object, Depends(graph_for)]
