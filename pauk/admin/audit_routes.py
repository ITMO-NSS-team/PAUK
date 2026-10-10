"""The change feed as a page: filters, paging, and one entity's history."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from pauk.admin import feed
from pauk.admin.deps import CurrentUser, Db, Session, templates
from pauk.graph.mutations import NODE_FIELDS

router = APIRouter()


@router.get("/audit", response_class=HTMLResponse)
def changes(request: Request, user: CurrentUser, session: Session, db: Db,
            actor: str = "", entity_type: str = "", entity_id: str = "",
            kind: str = "", since: str = "", until: str = "",
            order: str = "new", page: int = 1):
    """The change feed, readable by anyone who can sign in, viewers included."""
    page = max(page, 1)
    filters = {"actor": actor, "entity_type": entity_type, "entity_id": entity_id,
               "kind": kind, "since": since, "until": until}
    rows = feed.entries(db, **filters, skip=(page - 1) * feed.PAGE,
                        oldest_first=order == "old")
    total = feed.count(db, **filters)
    return templates.TemplateResponse(request, "audit.html", {
        "user": user, "csrf": session["csrf"], "rows": rows, "total": total,
        "page": page, "pages": max((total + feed.PAGE - 1) // feed.PAGE, 1),
        "filters": filters, "order": order, "actors": feed.actors(db),
        "entity_types": feed.entity_types(db), "kinds": feed.KINDS,
        "labels": sorted(NODE_FIELDS)})
