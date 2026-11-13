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
    NotFound,
    VersionConflict,
    create_node,
    create_relationship,
    delete_node,
    delete_relationship,
    normalize_fields,
    read_node,
    read_relationship,
    update_node,
    validate_label,
    validate_relationship,
)
from pauk.graph.overrides import (
    COLLECTION,
    CREATE,
    DELETE,
    LINK,
    SET,
    DecisionWriteConflict,
    apply_overrides,
    deactivate_override,
    deactivate_relationship_override,
    override_id,
    relationship_override_id,
)

router = APIRouter()


@router.get("/overrides", response_class=HTMLResponse)
def in_force(request: Request, user: CurrentUser, session: Session, db: Db,
             tab: str = "list", page: int = 1):
    """Decisions kept so a publish cannot undo them, and their conflicts."""
    page = max(page, 1)
    skip = (page - 1) * decisions.PAGE
    total = decisions.count_in_force(db)
    # Both tabs need the count and the pass is not cheap.
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
    """Restore the graph before withdrawing the decision, compensating failed writes."""
    form = await request.form()
    kind = str(form.get("kind") or "node")
    if kind not in ("node", "rel"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "неизвестный вид решения")
    try:
        if kind == "rel":
            triple = (str(form["src_label"]), str(form["rel_type"]), str(form["tgt_label"]))
            src_id, tgt_id = str(form["src_id"]), str(form["target_id"])
            validate_relationship(*triple)
            document_id = relationship_override_id(*triple, src_id, tgt_id)
        else:
            label, node_id = str(form["label"]), str(form["target_id"])
            validate_label(label)
            document_id = override_id(label, node_id)
    except KeyError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "не хватает данных о решении") from None
    except MutationError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from None
    row = db[COLLECTION].find_one({"_id": document_id, "active": True})
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "такого решения нет")
    op = row["op"]
    if op in (CREATE, LINK):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "заведённые вручную записи и связи снимают на карточке записи")
    version = row.get("write_id") or ""
    if ("seen_write" not in form or str(form["seen_write"]) != version
            or (form.get("op") and str(form["op"]) != op)):
        raise HTTPException(status.HTTP_409_CONFLICT, "Решение изменилось. Обновите страницу перед отменой.")

    def withdraw():
        if kind == "rel":
            dropped = deactivate_relationship_override(db, *triple, src_id, tgt_id,
                                                       only_op=op, expected_write_id=version)
        else:
            dropped = deactivate_override(db, label, node_id, only_op=op, expected_write_id=version)
        if not dropped:
            raise DecisionWriteConflict("the decision changed before withdrawal")

    def compensate():
        pass
    restored = ""
    try:
        if op == SET and kind == "node":
            back = normalize_fields(label, decisions.source_of_truth(db, label, node_id), from_snapshot=True)
            if back:
                try:
                    before = read_node(graph, label, node_id)
                except NotFound:
                    before = None
                if before is not None:
                    after = update_node(graph, label, node_id, back,
                                        expected_updated_at=before.get("updated_at"), from_snapshot=True)
                    def compensate():
                        update_node(graph, label, node_id, {name: before.get(name) for name in back},
                                    expected_updated_at=after["updated_at"], from_snapshot=True)
                    restored = "field"
        elif op == DELETE and kind == "rel":
            try:
                read_relationship(graph, *triple, src_id, tgt_id)
            except NotFound:
                snapshot = decisions.deleted_relationship_properties(db, *triple, src_id, tgt_id)
                create_relationship(graph, *triple, src_id, tgt_id, snapshot)
                def compensate():
                    delete_relationship(graph, *triple, src_id, tgt_id)
                restored = "link"
        elif op == DELETE:
            snapshot = decisions.deleted_fields(db, label, node_id)
            if snapshot:
                try:
                    read_node(graph, label, node_id)
                except NotFound:
                    create_node(graph, label, node_id,
                                {name: value for name, value in snapshot.items() if name in NODE_FIELDS[label]},
                                from_snapshot=True)
                    def compensate():
                        delete_node(graph, label, node_id)
                    restored = "node"
        decisions.record_change(undo=compensate, write=withdraw)
        if kind == "node":
            apply_overrides(graph, db, document_id=document_id)
    except VersionConflict:
        raise HTTPException(status.HTTP_409_CONFLICT, "Запись изменилась. Обновите страницу перед отменой.") from None
    except MutationError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from None
    return RedirectResponse(
        f"/overrides?undone={restored or 1}&tab={form.get('tab', 'list')}",
        status_code=status.HTTP_303_SEE_OTHER)
