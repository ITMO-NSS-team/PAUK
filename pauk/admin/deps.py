"""Wiring shared by every route: the database, the graph, the caller.

Neo4j is not reachable from outside the perimeter, so every write goes through
`pauk.graph.mutations`. Routes never receive a raw driver, only the audited
client, which records who did what.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.templating import Jinja2Templates
from neo4j.exceptions import AuthError, ServiceUnavailable
from pymongo.database import Database
from pymongo.errors import PyMongoError

from pauk.admin.auth import COOKIE, User, check_csrf, read_session
from pauk.jobs import store
from pauk.jobs.models import GRAPH, aware, now
from pauk.settings import Settings


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_config(request: Request) -> Settings:
    return request.app.state.config


#: How long the panel waits for Mongo; the driver's own default is thirty seconds.
MONGO_TIMEOUT_MS = 2000

#: Said when Mongo cannot be reached: that is the whole panel, not one page.
MONGO_SILENT = "MongoDB не отвечает. Панель без неё работать не может."


def get_session(request: Request, db: Annotated[Database, Depends(get_db)]) -> dict | None:
    """The caller's session, or None when there is not one.

    Raises:
        HTTPException: 503 when Mongo cannot be reached. Sessions live there,
            so an unreachable Mongo is not an anonymous visitor.
    """
    try:
        return read_session(db, request.cookies.get(COOKIE))
    except PyMongoError as error:
        logger.warning("mongo is not answering, no session to read: %s", error)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, MONGO_SILENT) from None


def require_user(session: Annotated[dict | None, Depends(get_session)]) -> User:
    """The signed-in caller.

    Raises:
        HTTPException: 401 when there is no live session.
    """
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "sign in first")
    return User(login=session["login"], role=session["role"])


def require_editor(user: Annotated[User, Depends(require_user)]) -> User:
    """A caller allowed to change the graph, as opposed to read it."""
    if not user.can_write:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this account can only read")
    return user


def require_stores(db: Annotated[Database, Depends(get_db)]) -> None:
    """Refuse a write when the store that has to record it is not answering.

    A graph change is protected only by a decision in Mongo, and there is no
    transaction across the two. This catches sustained Mongo outages; a failure
    in the instant between check and write is handled by putting the graph
    back (see `pauk.admin.nodes._record`).

    Raises:
        HTTPException: 503.
    """
    try:
        db.client.admin.command("ping")
    except PyMongoError as error:
        logger.warning("mongo is not answering: %s", error)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Mongo не отвечает: правка не сохранилась бы как решение") from None


def require_admin(user: Annotated[User, Depends(require_user)]) -> User:
    """A caller allowed to set the pipeline going.

    Raises:
        HTTPException: 403 for anyone else. A publish rewrites the whole graph
            and a collection run spends hours of API quota.
    """
    if not user.can_run:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "starting a run needs the admin role")
    return user


async def require_csrf(request: Request,
                       session: Annotated[dict | None, Depends(get_session)]) -> None:
    """Reject a form that did not come from our own page.

    The session cookie travels with a cross-site POST, so only a token the
    other site cannot read proves origin. The body is read here so the check
    runs before the route sees anything.
    """
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "sign in first")
    form = await request.form()
    if not check_csrf(session, form.get("csrf")):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "stale form, open the page again")


def graph_for(request: Request, user: Annotated[User, Depends(require_user)]) -> Iterator:
    """An audited graph client with the caller's name attached.

    The driver is shared by the application; the audited wrapper is per request.

    Raises:
        HTTPException: 503 when the graph cannot be reached (no password
            configured, or nothing listening).
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


def graph_if_up(request: Request, user: Annotated[User, Depends(require_user)]) -> Iterator:
    """The audited client, or None when the graph is not answering.

    For routes that are still useful without the graph: a review answer is
    written to Mongo and read by the rules on their next run.
    """
    try:
        client = request.app.state.graph.audited(actor=user.actor, source="admin-ui")
    except (ValueError, ServiceUnavailable, AuthError) as error:
        logger.warning("graph unavailable, carrying on without it: %s", error)
        yield None
        return
    try:
        yield client
    finally:
        client.close()


logger = logging.getLogger("pauk.admin")

def plural(count: int, one: str, few: str, many: str) -> str:
    """Russian noun agreement: 1 узел, 2 узла, 5 узлов."""
    if count % 10 == 1 and count % 100 != 11:
        return one
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return few
    return many


def moment(value) -> str:
    """A stored time as "2026-08-28 14:03:11", or "" when there is none.

    Job times are datetimes; a bare str() carries microseconds and a timezone.
    """
    if value is None:
        return ""
    return value.strftime("%Y-%m-%d %H:%M:%S")


def ago(value) -> str:
    """How long ago, in words: "только что", "вчера в 18:24", "23.09 в 18:24".

    Takes a datetime (from the queue) or an ISO string (from the health snapshot).
    """
    if not value:
        return ""
    moment_at = datetime.fromisoformat(value) if isinstance(value, str) else value
    moment_at = aware(moment_at)
    minutes = int((now() - moment_at).total_seconds() // 60)
    if minutes < 1:
        return "только что"
    if minutes < 60:
        return f"{minutes} {plural(minutes, 'минуту', 'минуты', 'минут')} назад"
    local = moment_at.astimezone()
    today = now().astimezone().date()
    if local.date() == today:
        return f"сегодня в {local:%H:%M}"
    if (today - local.date()).days == 1:
        return f"вчера в {local:%H:%M}"
    return f"{local:%d.%m} в {local:%H:%M}"


def running_job(request: Request) -> dict:
    """The graph job under way, for the warning strip on every page.

    A context processor so no route can forget to pass it. A queue failure
    leaves the strip off rather than taking the page down.
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
templates.env.filters["ago"] = ago


def job_words(kind) -> str:
    """A job kind in the words the panel uses for it.

    Imported late because `job_routes` reads `templates` from here.
    """
    from pauk.admin.job_routes import KINDS
    return KINDS.get(kind, str(kind))


templates.env.filters["job_words"] = job_words


# Length past which a value arrives folded; the script drops a needless button.
LONG_VALUE = 160


def safe_path(target: str, default: str = "/") -> str:
    """A path from the query string, refused if it leaves this site.

    Guards against `?next=https://evil.example` open redirects.
    """
    if not target.startswith("/") or target.startswith("//"):
        return default
    return target


def is_long(value) -> bool:
    """Whether a value should arrive folded.

    Decided server-side so long text does not paint in full and then collapse.
    """
    return value is not None and len(str(value)) > LONG_VALUE


templates.env.filters["is_long"] = is_long

# Named aliases so routes read as `db: Db`, not the whole Annotated form.
Db = Annotated[Database, Depends(get_db)]
Config = Annotated[Settings, Depends(get_config)]
Session = Annotated[dict | None, Depends(get_session)]
CurrentUser = Annotated[User, Depends(require_user)]
Editor = Annotated[User, Depends(require_editor)]
Admin = Annotated[User, Depends(require_admin)]
CsrfChecked = Annotated[None, Depends(require_csrf)]
StoresReady = Annotated[None, Depends(require_stores)]
Graph = Annotated[object, Depends(graph_for)]
MaybeGraph = Annotated[object | None, Depends(graph_if_up)]
