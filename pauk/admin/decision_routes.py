"""Manual decisions as a page: what is in force, and what the source disputes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from pauk.admin import decisions
from pauk.admin.deps import (
    CsrfChecked,
    CurrentUser,
    Db,
    Editor,
    Graph,
    Session,
    StoresReady,
    templates,
)
from pauk.graph.mutations import (
    NODE_FIELDS,
    MutationError,
    create_node,
    create_relationship,
    update_node,
)
from pauk.graph.overrides import (
    CREATE,
    DELETE,
    LINK,
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
    total = decisions.count_in_force(db)
    # Walked once: both tabs need the count, and the pass is not cheap.
    disputed_rows = decisions.conflicts(db, limit=None)
    disputed = len(disputed_rows)
    shown = disputed if tab == "conflicts" else total
    return templates.TemplateResponse(request, "overrides.html", {
        "user": user, "csrf": session["csrf"], "tab": tab, "page": page,
        "pages": max((shown + decisions.PAGE - 1) // decisions.PAGE, 1),
        "rows": decisions.in_force(db, skip=skip) if tab != "conflicts" else [],
        "conflicts": disputed_rows[skip:skip + decisions.PAGE] if tab == "conflicts" else [],
        "total": total, "disputed": disputed})


@router.post("/overrides/undo")
async def undo(request: Request, user: Editor, db: Db, graph: Graph,
               _: CsrfChecked, __: StoresReady):
    """Stop applying one decision, keeping the record that it was made.

    The graph is not put back by hand: the decision is switched off and
    the rest are reapplied, so the field returns to whatever the pipeline
    last wrote — which is the point of undoing rather than editing back.
    """
    form = await request.form()
    kind = str(form.get("kind", "node"))
    op = str(form.get("op", ""))
    # Read before withdrawing: it rewrites the decision both are read from.
    snapshot: dict = {}
    back: dict = {}
    try:
        if kind == "rel":
            if op == LINK:
                # A claim, not an instruction: it is taken back by unlinking.
                raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                    "добавленную вручную связь снимают на карточке записи")
            triple = (str(form["src_label"]), str(form["rel_type"]), str(form["tgt_label"]))
            src_id, tgt_id = str(form["src_id"]), str(form["target_id"])
            dropped = deactivate_relationship_override(db, *triple, src_id, tgt_id,
                                                       only_op=DELETE)
        else:
            label, node_id = str(form["label"]), str(form["target_id"])
            if op == CREATE:
                # A claim, like a link: taken back by deleting the record.
                raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                    "заведённую вручную запись снимают удалением на её карточке")
            if op == DELETE:
                snapshot = decisions.deleted_fields(db, label, node_id)
            elif op == SET:
                back = decisions.source_of_truth(db, label, node_id)
            # Only the decision the page showed: it may be another one by now.
            dropped = deactivate_override(db, label, node_id, only_op=op or None)
    except KeyError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "не хватает данных о решении") from None
    if not dropped:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "такого решения нет")

    # Lifting the ban creates nothing, so the record is restored here.
    restored = ""
    try:
        if op == SET and kind == "node":
            # The same for a field: nothing else would put it back.
            if back:
                update_node(graph, label, node_id, back)
                restored = "field"
        elif op == DELETE and kind == "rel":
            create_relationship(graph, *triple, src_id, tgt_id)
            restored = "link"
        elif op == DELETE and snapshot:
            create_node(graph, label, node_id,
                        {name: value for name, value in snapshot.items()
                         if name in NODE_FIELDS[label]})
            restored = "node"
        apply_overrides(graph, db)
    except MutationError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from None
    return RedirectResponse(
        f"/overrides?undone={restored or 1}&tab={form.get('tab', 'list')}",
        status_code=status.HTTP_303_SEE_OTHER)
