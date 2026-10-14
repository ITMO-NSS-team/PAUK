"""The panel itself: a FastAPI service, separate from the public map.

The map is a static site (`pauk/gui/web`) served read-only on its own port.
This service holds the only routes that write and runs next to the database,
not on the public interface.

Start it with:

    uv run uvicorn pauk.admin.app:build --factory --port 8600

Accounts come from `pauk admin user add`; there is no default login.
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
from pymongo.errors import PyMongoError

from pauk.admin import (
    audit_routes,
    decision_routes,
    health_routes,
    job_routes,
    nodes,
    review_routes,
    summary,
)
from pauk.admin.auth import (
    COOKIE,
    SESSION_HOURS,
    AuthError,
    TooManyAttempts,
    User,
    authenticate,
    close_session,
    open_session,
    read_session,
)
from pauk.admin.deps import (
    MONGO_SILENT,
    MONGO_TIMEOUT_MS,
    CsrfChecked,
    CurrentUser,
    Db,
    Session,
    safe_path,
    templates,
)
from pauk.graph.audit import SharedGraph
from pauk.graph.mutations import NODE_FIELDS, RELATIONSHIPS, count_nodes
from pauk.settings import Settings
from pauk.storage import get_mongo_client

logger = logging.getLogger("pauk.admin")

# How long the overview waits for the graph before dropping the counts.
COUNT_TIMEOUT = 2.0


class _LazyGraph:
    """The shared driver, opened on first use and kept afterwards.

    Not opened at startup so an unreachable graph cannot stop sign-in.
    """

    def __init__(self, config: Settings, db) -> None:
        self._config, self._db, self._shared = config, db, None
        # Sync routes run in a threadpool: two first requests really do race.
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
    """Close the shared driver when the service stops."""
    yield
    app.state.graph.close()


def _node_counts(graph: _LazyGraph) -> dict[str, int] | None:
    """How many nodes of each label there are, or None if the graph is silent.

    The driver is told to connect quickly and not retry, so a person waiting
    for the page is not blocked while the database backs off.
    """
    try:
        # The shared driver, so the overview does not open a pool of its own.
        return count_nodes(graph.audited(actor="panel", source="admin-ui"))
    except Exception as error:  # the overview works without a graph
        logger.info("overview without counts: %s", error)
        return None


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
    # Short timeout, unlike the pipeline's: a waiting web request reads as hung.
    app.state.db = (db if db is not None
                    else get_mongo_client(config, timeout_ms=MONGO_TIMEOUT_MS)
                    [config.mongo_db])
    # One driver for the service, opened lazily: signing in needs only Mongo.
    app.state.graph = _LazyGraph(config, app.state.db)

    @app.exception_handler(status.HTTP_401_UNAUTHORIZED)
    async def unauthorized(request: Request, exc: HTTPException):
        """Send a browser to the login page instead of showing it raw JSON.

        Browsers are told apart by their Accept header; the wanted page is
        carried along so the login can return to it.
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
        # An unreachable Mongo is what this page reports; reading it would raise.
        try:
            session = read_session(request.app.state.db, request.cookies.get(COOKIE))
        except PyMongoError:
            session = None
        return templates.TemplateResponse(
            request, "unavailable.html",
            {"user": User(login=session["login"], role=session["role"]) if session else None,
             "csrf": session["csrf"] if session else "", "detail": exc.detail},
            status_code=exc.status_code)

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, session: Session, next: str = "/"):
        if session is not None:
            return RedirectResponse(safe_path(next), status_code=status.HTTP_303_SEE_OTHER)
        return templates.TemplateResponse(request, "login.html",
                                          {"user": None, "next": safe_path(next)})

    @app.post("/login")
    def login(request: Request, db: Db,
              login: Annotated[str, Form()], password: Annotated[str, Form()],
              next: Annotated[str, Form()] = "/"):
        # No CSRF check: no session yet, and a forged login costs the attacker.
        def refused(message: str, *, denied: bool, code: int):
            return templates.TemplateResponse(
                request, "login.html",
                {"user": None, "error": message, "denied": denied,
                 "next": safe_path(next)},
                status_code=code)

        try:
            user = authenticate(db, login, password)
            token = open_session(db, user)
        except TooManyAttempts as error:
            # Told plainly, unlike a wrong password: the wait is not a secret.
            logger.info("locked-out login attempt for %r", login)
            return refused(f"Слишком много попыток. Попробуйте через {error.minutes} мин.",
                           denied=False, code=status.HTTP_429_TOO_MANY_REQUESTS)
        except AuthError:
            logger.info("failed login for %r", login)
            return refused("", denied=True, code=status.HTTP_401_UNAUTHORIZED)
        except PyMongoError as error:
            # Accounts live in Mongo: this is a service down, not a bad password.
            logger.warning("mongo is not answering, cannot sign anybody in: %s", error)
            return refused(MONGO_SILENT, denied=False,
                           code=status.HTTP_503_SERVICE_UNAVAILABLE)
        response = RedirectResponse(safe_path(next), status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(
            COOKIE, token,
            max_age=SESSION_HOURS * 3600,
            httponly=True,      # a script on the page must not be able to read it
            samesite="lax",     # not sent along with a cross-site POST
            secure=config.admin_secure_cookie,
            path="/")
        return response

    @app.post("/logout")
    def logout(request: Request, db: Db, _: CsrfChecked):
        """End the session; the CSRF token is checked like on every other form."""
        close_session(db, request.cookies.get(COOKIE))
        response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, user: CurrentUser, session: Session, db: Db):
        counts = _node_counts(app.state.graph)
        labels = [(label, len(NODE_FIELDS[label]), (counts or {}).get(label))
                  for label in sorted(NODE_FIELDS)]
        return templates.TemplateResponse(request, "index.html", {
            "user": user, "csrf": session["csrf"], "counted": counts is not None,
            "labels": labels, "relationships": len(RELATIONSHIPS),
            "summary": summary.collect(db)})

    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

    # Version by mtime: browsers hold CSS until the address changes.
    def stylesheet() -> str:
        css = Path(__file__).parent / "static" / "panel.css"
        return f"/static/panel.css?v={int(css.stat().st_mtime) if css.is_file() else 0}"

    templates.env.globals["stylesheet"] = stylesheet

    # The logo and fonts live in the panel's own static/ folder.
    static = Path(__file__).parent / "static"
    for name in ("fonts", "icons"):
        source = static / name
        if source.is_dir():
            app.mount(f"/assets/{name}", StaticFiles(directory=str(source)), name=name)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        """The map's tab icon, served as a file.

        Browsers request /favicon.ico on their own, so it must be the same
        image the <link> tag points at. A file rather than a redirect, since a
        301 is cached too hard.
        """
        path = static / "icons" / "pauk-frame.png"
        if not path.is_file():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "the icon is missing")
        return FileResponse(path, media_type="image/png",
                            headers={"Cache-Control": "public, max-age=86400"})

    app.include_router(nodes.router)
    app.include_router(audit_routes.router)
    app.include_router(decision_routes.router)
    app.include_router(job_routes.router)
    app.include_router(review_routes.router)
    app.include_router(health_routes.router)
    return app
