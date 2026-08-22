"""Manual decisions as a page: what is in force, and what the source disputes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from pauk.admin import decisions
from pauk.admin.deps import CsrfChecked, CurrentUser, Db, Editor, Graph, Session, templates
from pauk.graph.mutations import MutationError
from pauk.graph.overrides import (
    apply_overrides,
    deactivate_override,
    deactivate_relationship_override,
)

router = APIRouter()


@router.get("/overrides", response_class=HTMLResponse)
def in_force(request: Request, user: CurrentUser, session: Session, db: Db, tab: str = "list"):
    """Decisions kept so a publish cannot undo them, and their conflicts.

    One page with two tabs rather than two pages: both read the same
    documents, and the question "what did we decide" and "what does the
    source now disagree with" are asked one after the other.
    """
    return templates.TemplateResponse(request, "overrides.html", {
        "user": user, "csrf": session["csrf"], "tab": tab,
        "rows": decisions.in_force(db) if tab != "conflicts" else [],
        "conflicts": decisions.conflicts(db) if tab == "conflicts" else [],
        "total": decisions.count_in_force(db),
        "disputed": decisions.count_conflicts(db)})


@router.post("/overrides/undo")
async def undo(request: Request, user: Editor, db: Db, graph: Graph, _: CsrfChecked):
    """Stop applying one decision, keeping the record that it was made.

    The graph is not put back by hand: the decision is switched off and
    the rest are reapplied, so the field returns to whatever the pipeline
    last wrote — which is the point of undoing rather than editing back.
    """
    form = await request.form()
    kind = str(form.get("kind", "node"))
    try:
        if kind == "rel":
            dropped = deactivate_relationship_override(
                db, str(form["src_label"]), str(form["rel_type"]), str(form["tgt_label"]),
                str(form["src_id"]), str(form["target_id"]))
        else:
            dropped = deactivate_override(db, str(form["label"]), str(form["target_id"]))
    except KeyError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "не хватает данных о решении") from None
    if not dropped:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "такого решения нет")
    try:
        apply_overrides(graph, db)
    except MutationError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from None
    return RedirectResponse(f"/overrides?undone=1&tab={form.get('tab', 'list')}",
                            status_code=status.HTTP_303_SEE_OTHER)
