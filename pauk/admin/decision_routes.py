"""Manual decisions as a page: what is in force, and what the source disputes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from pauk.admin import decisions
from pauk.admin.deps import CsrfChecked, CurrentUser, Db, Editor, Graph, Session, templates
from pauk.graph.mutations import (
    NODE_FIELDS,
    MutationError,
    create_node,
    create_relationship,
    update_node,
)
from pauk.graph.overrides import (
    DELETE,
    SET,
    apply_overrides,
    deactivate_override,
    deactivate_relationship_override,
)

router = APIRouter()


@router.get("/overrides", response_class=HTMLResponse)
def in_force(request: Request, user: CurrentUser, session: Session, db: Db,
             tab: str = "list", page: int = 1):
    """Decisions kept so a publish cannot undo them, and their conflicts.

    One page with two tabs rather than two pages: both read the same
    documents, and the question "what did we decide" and "what does the
    source now disagree with" are asked one after the other.
    """
    page = max(page, 1)
    skip = (page - 1) * decisions.PAGE
    total, disputed = decisions.count_in_force(db), decisions.count_conflicts(db)
    shown = disputed if tab == "conflicts" else total
    return templates.TemplateResponse(request, "overrides.html", {
        "user": user, "csrf": session["csrf"], "tab": tab, "page": page,
        "pages": max((shown + decisions.PAGE - 1) // decisions.PAGE, 1),
        "rows": decisions.in_force(db, skip=skip) if tab != "conflicts" else [],
        "conflicts": decisions.conflicts(db, skip=skip) if tab == "conflicts" else [],
        "total": total, "disputed": disputed})


@router.post("/overrides/undo")
async def undo(request: Request, user: Editor, db: Db, graph: Graph, _: CsrfChecked):
    """Stop applying one decision, keeping the record that it was made.

    The graph is not put back by hand: the decision is switched off and
    the rest are reapplied, so the field returns to whatever the pipeline
    last wrote — which is the point of undoing rather than editing back.
    """
    form = await request.form()
    kind = str(form.get("kind", "node"))
    op = str(form.get("op", ""))
    try:
        if kind == "rel":
            triple = (str(form["src_label"]), str(form["rel_type"]), str(form["tgt_label"]))
            src_id, tgt_id = str(form["src_id"]), str(form["target_id"])
            dropped = deactivate_relationship_override(db, *triple, src_id, tgt_id)
        else:
            label, node_id = str(form["label"]), str(form["target_id"])
            dropped = deactivate_override(db, label, node_id)
    except KeyError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "не хватает данных о решении") from None
    if not dropped:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "такого решения нет")

    # Withdrawing a deletion lifts the ban but does not put the record
    # back: apply_overrides applies what is in force, and "no longer
    # deleted" is not an instruction to create anything. Left at that, the
    # button looks broken — the record would reappear only at the next
    # publish, and only if the pipeline still knows it. So undo restores it
    # here, from what the deletion recorded.
    restored = ""
    try:
        if op == SET and kind == "node":
            # Withdrawing an edit has to put the field back, not merely
            # stop reapplying it. apply_overrides applies what is in force;
            # a withdrawn decision is not an instruction to restore
            # anything, so the hand-written value would sit in the graph
            # until a publish happened to touch that field — and if the
            # record drops out of the pipeline's scope, forever.
            back = decisions.source_of_truth(db, label, node_id)
            if back:
                update_node(graph, label, node_id, back)
                restored = "field"
        elif op == DELETE and kind == "rel":
            create_relationship(graph, *triple, src_id, tgt_id)
            restored = "link"
        elif op == DELETE:
            fields = decisions.deleted_fields(db, label, node_id)
            if fields:
                create_node(graph, label, node_id,
                            {name: value for name, value in fields.items()
                             if name in NODE_FIELDS[label]})
                restored = "node"
        apply_overrides(graph, db)
    except MutationError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from None
    return RedirectResponse(
        f"/overrides?undone={restored or 1}&tab={form.get('tab', 'list')}",
        status_code=status.HTTP_303_SEE_OTHER)
