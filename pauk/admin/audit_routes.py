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
            kind: str = "", page: int = 1):
    """The feed. Readable by anyone who can sign in, including viewers.

    Reading who changed what is not a privilege: the feed is how a wrong
    value gets explained, and a viewer looking at a suspicious field needs
    it as much as an editor does.
    """
    page = max(page, 1)
    filters = {"actor": actor, "entity_type": entity_type,
               "entity_id": entity_id, "kind": kind}
    rows = feed.entries(db, **filters, skip=(page - 1) * feed.PAGE)
    total = feed.count(db, actor=actor, entity_type=entity_type,
                       entity_id=entity_id, change_kind=kind)
    return templates.TemplateResponse(request, "audit.html", {
        "user": user, "csrf": session["csrf"], "rows": rows, "total": total,
        "page": page, "pages": max((total + feed.PAGE - 1) // feed.PAGE, 1),
        "filters": filters, "actors": feed.actors(db),
        "entity_types": feed.entity_types(db), "kinds": feed.KINDS,
        "labels": sorted(NODE_FIELDS)})
