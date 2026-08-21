"""Wiring shared by every route: the database, the graph, the caller.

The rule this module enforces is the one that matters most for the panel.
Neo4j is not reachable from outside the perimeter, so the panel is the
only way to write to the graph, and every write has to go through
`pauk.graph.mutations`. Routes therefore never receive a raw driver —
they receive the audited client, and the audit sink records who did what
while `actor_context` is held.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.templating import Jinja2Templates
from pymongo.database import Database

from pauk.admin.auth import COOKIE, User, check_csrf, read_session
from pauk.graph.audit import actor_context, audited_client
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

    Opened per request rather than kept on the app: a driver shared across
    requests would report whichever actor happened to be set last.
    """
    client = audited_client(request.app.state.config, request.app.state.db)
    try:
        with actor_context(user.actor, source="admin-ui"):
            yield client
    finally:
        client.close()


# Named aliases so routes read as `db: Db` instead of repeating the
# Annotated form in every signature.
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

Db = Annotated[Database, Depends(get_db)]
Config = Annotated[Settings, Depends(get_config)]
Session = Annotated[dict | None, Depends(get_session)]
CurrentUser = Annotated[User, Depends(require_user)]
Editor = Annotated[User, Depends(require_editor)]
CsrfChecked = Annotated[None, Depends(require_csrf)]
Graph = Annotated[object, Depends(graph_for)]
