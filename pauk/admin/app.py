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
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pymongo.database import Database

from pauk.admin import nodes
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
from pauk.graph.audit import audited_client
from pauk.graph.mutations import NODE_FIELDS, RELATIONSHIPS, count_nodes
from pauk.settings import Settings
from pauk.storage import get_mongo_client

logger = logging.getLogger("pauk.admin")

# How long the overview waits for the graph before dropping the counts.
COUNT_TIMEOUT = 2.0


def _node_counts(config: Settings, db) -> dict[str, int] | None:
    """How many nodes of each label there are, or None if the graph is silent.

    The count is a nicety on an overview page, so the driver is told to
    connect quickly and not to retry: retries suit a batch job, while a
    person waiting for a page should be told at once that the graph is not
    answering. Without this the page blocks for as long as the database
    stays unreachable — the driver backs off for tens of seconds.
    """
    try:
        client = audited_client(config, db, connection_timeout=COUNT_TIMEOUT, retry_time=0)
    except Exception as error:  # noqa: BLE001 — the overview works without a graph
        logger.info("overview without counts: %s", error)
        return None
    try:
        return count_nodes(client)
    except Exception as error:  # noqa: BLE001
        logger.info("overview without counts: %s", error)
        return None
    finally:
        client.close()


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
    app = FastAPI(title="PAUK admin", docs_url=None, redoc_url=None)
    app.state.config = config
    app.state.db = db if db is not None else get_mongo_client(config)[config.mongo_db]

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
        counts = _node_counts(config, app.state.db)
        labels = [(label, len(NODE_FIELDS[label]), (counts or {}).get(label))
                  for label in sorted(NODE_FIELDS)]
        return templates.TemplateResponse(request, "index.html", {
            "user": user, "csrf": session["csrf"], "counted": counts is not None,
            "labels": labels, "relationships": len(RELATIONSHIPS)})

    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

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

        A redirect gets cached hard by browsers, so a 404 caught once would
        outlive the fix.
        """
        path = web / "vendor" / "icons" / "pauk-frame.png"
        if not path.is_file():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "the icon is missing")
        return FileResponse(path, media_type="image/png",
                            headers={"Cache-Control": "public, max-age=86400"})

    app.include_router(nodes.router)
    return app
