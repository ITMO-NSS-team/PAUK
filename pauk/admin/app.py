"""The panel itself: a FastAPI service, separate from the public map.

`pauk/gui/serve.py` keeps serving the map as read-only static files on its
own port. This service is the only one with routes that write, and it runs
next to the database rather than on the public interface — Neo4j is not
exposed, so there is no other way in and nothing to guard on the map side.

Start it with:

    uv run uvicorn pauk.admin.app:build --factory --port 8600

Accounts come from `pauk admin user add`; there is no default login and no
way to create one from the browser.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pymongo.database import Database

from pauk.admin import audit_routes, decision_routes, nodes
from pauk.admin.auth import (
    COOKIE,
    SESSION_HOURS,
    AuthError,
    User,
    authenticate,
    close_session,
    open_session,
    read_session,
)
from pauk.admin.deps import CurrentUser, Db, Session, templates
from pauk.graph.audit import SharedGraph
from pauk.graph.mutations import NODE_FIELDS, RELATIONSHIPS, count_nodes
from pauk.settings import Settings
from pauk.storage import get_mongo_client

logger = logging.getLogger("pauk.admin")

# How long the overview waits for the graph before dropping the counts.
COUNT_TIMEOUT = 2.0


class _LazyGraph:
    """The shared driver, opened on first use and kept afterwards.

    Not opened at startup: an unreachable graph must not stop the panel
    from starting, and a service that cannot sign anyone in is worse than
    one whose node screens say the database is quiet.
    """

    def __init__(self, config: Settings, db) -> None:
        self._config, self._db, self._shared = config, db, None
        # Routes are sync, so they run in a threadpool and the first two
        # requests really do arrive together. Without the lock both see no
        # driver, both build one, and the loser's connection pool is left
        # open with nothing holding it.
        self._lock = threading.Lock()

    def audited(self, **who):
        if self._shared is None:
            with self._lock:
                if self._shared is None:
                    self._shared = SharedGraph(self._config, self._db,
                                               connection_timeout=COUNT_TIMEOUT, retry_time=0)
        return self._shared.audited(**who)

    def close(self) -> None:
        if self._shared is not None:
            self._shared.close()
            self._shared = None


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Hand the driver back when the service stops.

    One driver serves the whole panel and no caller closes it — that is the
    point of sharing it. Which leaves exactly one place where it has to be
    closed, and this is it: without this the pool outlives the application
    object, and a reload leaves the previous one holding its connections.
    """
    yield
    app.state.graph.close()


def _node_counts(graph: _LazyGraph) -> dict[str, int] | None:
    """How many nodes of each label there are, or None if the graph is silent.

    The count is a nicety on an overview page, so the driver is told to
    connect quickly and not to retry: retries suit a batch job, while a
    person waiting for a page should be told at once that the graph is not
    answering. Without this the page blocks for as long as the database
    stays unreachable — the driver backs off for tens of seconds.
    """
    try:
        # The same shared driver every other page uses: the overview used to
        # open a second one, so landing on the front page cost two pools.
        return count_nodes(graph.audited(actor="panel", source="admin-ui"))
    except Exception as error:  # the overview works without a graph
        logger.info("overview without counts: %s", error)
        return None


def _safe_next(target: str) -> str:
    """Where to go after signing in, refusing anywhere but this site.

    Without the check, `?next=https://evil.example` would turn the login
    into an open redirect — a link that looks like ours and lands
    somewhere else.
    """
    if not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


def build(config: Settings | None = None, db: Database | None = None) -> FastAPI:
    """Assemble the application.

    Args:
        config: Settings; read from the environment when omitted.
        db: Mongo database. Injected by the tests; opened from the
            settings otherwise.
    """
    config = config or Settings()
    app = FastAPI(title="PAUK admin", docs_url=None, redoc_url=None, lifespan=_lifespan)
    app.state.config = config
    app.state.db = db if db is not None else get_mongo_client(config)[config.mongo_db]
    # One driver for the whole service, opened lazily: the panel has to
    # start without a graph, since signing in and the accounts live in
    # Mongo. `_lifespan` closes it when the service stops.
    app.state.graph = _LazyGraph(config, app.state.db)

    @app.exception_handler(status.HTTP_401_UNAUTHORIZED)
    async def unauthorized(request: Request, exc: HTTPException):
        """Send a browser to the login page instead of showing it raw JSON.

        A 401 is the right answer for a fetch or a script; a person who
        typed the address wants the form. The two are told apart by what
        the request says it accepts, and the page they wanted is carried
        along so the login can return them to it.
        """
        if "text/html" in request.headers.get("accept", ""):
            target = request.url.path
            if request.url.query:
                target = f"{target}?{request.url.query}"
            return RedirectResponse(f"/login?next={quote(target, safe='')}",
                                    status_code=status.HTTP_303_SEE_OTHER)
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    @app.exception_handler(status.HTTP_503_SERVICE_UNAVAILABLE)
    async def unavailable(request: Request, exc: HTTPException):
        """Show a person what is broken instead of a stack trace."""
        if "text/html" not in request.headers.get("accept", ""):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        session = read_session(request.app.state.db, request.cookies.get(COOKIE))
        return templates.TemplateResponse(
            request, "unavailable.html",
            {"user": User(login=session["login"], role=session["role"]) if session else None,
             "csrf": session["csrf"] if session else "", "detail": exc.detail},
            status_code=exc.status_code)

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, session: Session, next: str = "/"):
        if session is not None:
            return RedirectResponse(_safe_next(next), status_code=status.HTTP_303_SEE_OTHER)
        return templates.TemplateResponse(request, "login.html",
                                          {"user": None, "next": _safe_next(next)})

    @app.post("/login")
    def login(request: Request, db: Db,
              login: Annotated[str, Form()], password: Annotated[str, Form()],
              next: Annotated[str, Form()] = "/"):
        # No CSRF check here on purpose: there is no session yet to carry a
        # token, and a forged login only ever logs the victim in as the
        # attacker — the thing to prevent is a forged *edit*.
        try:
            user = authenticate(db, login, password)
        except AuthError as error:
            logger.info("failed login for %r", login)
            return templates.TemplateResponse(
                request, "login.html",
                {"user": None, "error": str(error), "next": _safe_next(next)},
                status_code=status.HTTP_401_UNAUTHORIZED)
        token = open_session(db, user)
        response = RedirectResponse(_safe_next(next), status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(
            COOKIE, token,
            max_age=SESSION_HOURS * 3600,
            httponly=True,      # a script on the page must not be able to read it
            samesite="lax",     # not sent along with a cross-site POST
            secure=config.admin_secure_cookie,
            path="/")
        return response

    @app.post("/logout")
    def logout(request: Request, db: Db):
        close_session(db, request.cookies.get(COOKIE))
        response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, user: CurrentUser, session: Session):
        counts = _node_counts(app.state.graph)
        labels = [(label, len(NODE_FIELDS[label]), (counts or {}).get(label))
                  for label in sorted(NODE_FIELDS)]
        return templates.TemplateResponse(request, "index.html", {
            "user": user, "csrf": session["csrf"], "counted": counts is not None,
            "labels": labels, "relationships": len(RELATIONSHIPS)})

    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

    # The stylesheet's version is its own mtime. Browsers hold CSS in cache
    # firmly, and a layout fix could fail to reach an open tab: the header
    # and the filters stayed in the old arrangement although the file had
    # already changed.
    def stylesheet() -> str:
        css = Path(__file__).parent / "static" / "panel.css"
        return f"/static/panel.css?v={int(css.stat().st_mtime) if css.is_file() else 0}"

    templates.env.globals["stylesheet"] = stylesheet

    # The logo and the fonts come from the map's own files instead of being
    # copied here: one place to update, and the panel looks like the same
    # product. Only these two paths are exposed — mounting the whole web
    # directory would serve the map's data dump from the admin port too.
    web = Path(__file__).resolve().parents[1] / "gui" / "web"
    for name in ("fonts", "icons"):
        source = web / "vendor" / name
        if source.is_dir():
            app.mount(f"/assets/{name}", StaticFiles(directory=str(source)), name=name)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        """The map's own tab icon, served as a file.

        The same image the <link> tag points at, and that matters: a browser
        asks for /favicon.ico on its own for the site root, and answering
        with a different picture there than on every other page is exactly
        how the icon ends up showing on /nodes/... but not on /.

        Served as a file rather than a redirect — a 301 gets cached hard
        enough to outlive the fix.
        """
        path = web / "vendor" / "icons" / "pauk-frame.png"
        if not path.is_file():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "the icon is missing")
        return FileResponse(path, media_type="image/png",
                            headers={"Cache-Control": "public, max-age=86400"})

    app.include_router(nodes.router)
    app.include_router(audit_routes.router)
    app.include_router(decision_routes.router)
    return app
