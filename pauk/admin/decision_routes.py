"""Manual decisions as a page: what is in force, and what the source disputes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from neo4j.exceptions import DriverError, Neo4jError
from pymongo.errors import PyMongoError

from pauk.admin import decisions
from pauk.admin.deps import (
    CsrfChecked,
    CurrentUser,
    Db,
    Editor,
    Graph,
    MaybeGraph,
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
    VERSION_RELATIONSHIP,
    DecisionWriteConflict,
    apply_overrides,
    deactivate_override,
    deactivate_relationship_override,
    override_id,
    relationship_override_id,
)
from pauk.graph.version_repair import apply_pending_version_repair_locked
from pauk.jobs.locks import Busy, held
from pauk.jobs.models import GRAPH
from pauk.storage import version_repair

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
        "total": total, "disputed": disputed,
        "pending_repairs": [{"collection": collection, "document_id": document_id, **intent}
                            for collection, document_id, intent in version_repair.pending(db)]})


@router.post("/overrides/retry-version")
async def retry_version(request: Request, user: Editor, db: Db, graph: MaybeGraph,
                        _: CsrfChecked, __: StoresReady):
    """Retry a saved obligation without changing any human decision."""
    form = await request.form()
    collection = str(form.get("collection", ""))
    document_id, token = str(form.get("document_id", "")), str(form.get("token", ""))
    try:
        intent = version_repair.load(db, collection, document_id, token)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "неизвестный источник согласования") from None
    if intent is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Согласование уже завершено или изменилось. Обновите страницу.")
    outcome = "pending"
    if graph is not None:
        try:
            with held(db, GRAPH):
                _, result = apply_pending_version_repair_locked(graph, db, collection, document_id, token)
            if result.get("version_repairs"):
                outcome = "applied"
        except (Busy, MutationError, Neo4jError, DriverError, PyMongoError):
            pass
    return RedirectResponse(f"/overrides?repair={outcome}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/overrides/undo")
async def undo(request: Request, user: Editor, db: Db, graph: Graph,
               _: CsrfChecked, __: StoresReady):
    """Keep the existing graph-required boundary for ordinary manual decisions."""
    return await _undo(request, user, db, graph)


@router.post("/overrides/undo-version")
async def undo_version(request: Request, user: Editor, db: Db, graph: MaybeGraph,
                       _: CsrfChecked, __: StoresReady):
    """A version decision can be saved durably even while Neo4j is unavailable."""
    form = await request.form()
    if (form.get("kind") != "rel" or tuple(str(form.get(key, "")) for key in
            ("src_label", "rel_type", "tgt_label")) != VERSION_RELATIONSHIP):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "ожидалась связь версий публикаций")
    return await _undo(request, user, db, graph)


async def _undo(request, user, db, graph):
    """Version undo commits before projection; generic edits keep compensation."""
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
    row = db[COLLECTION].find_one({"_id": document_id})
    version_edge = kind == "rel" and triple == VERSION_RELATIONSHIP
    seen = str(form.get("seen_write", ""))
    retry = bool(row and version_edge and "seen_write" in form
                 and row.get("version_undo_from") == seen and row.get("version_undo_intent"))
    if row is None or (not row.get("active") and not retry):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "такого решения нет")
    op = row["op"]
    if op in (CREATE, LINK) and not retry:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "заведённые вручную записи и связи снимают на карточке записи")
    version = seen if retry else row.get("write_id") or ""
    if ("seen_write" not in form or (not retry and seen != version)
            or (not retry and form.get("op") and str(form["op"]) != op)):
        raise HTTPException(status.HTTP_409_CONFLICT, "Решение изменилось. Обновите страницу перед отменой.")
    if graph is None and not version_edge:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Neo4j недоступен; решение не меняли.")

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
    version_deletion = version_edge and (op == DELETE or retry)
    try:
        if version_deletion:
            restored = decisions.undo_version_deletion(
                graph, db, src_id, tgt_id, document_id, withdraw, expected_write_id=version,
            )
        elif op == SET and kind == "node":
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
        if not version_deletion:
            decisions.record_change(undo=compensate, write=withdraw)
        if kind == "node":
            apply_overrides(graph, db, document_id=document_id)
    except Busy:
        raise HTTPException(status.HTTP_409_CONFLICT, "Граф занят. Повторите отмену после завершения операции.") from None
    except VersionConflict:
        raise HTTPException(status.HTTP_409_CONFLICT, "Запись изменилась. Обновите страницу перед отменой.") from None
    except MutationError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from None
    return RedirectResponse(
        f"/overrides?undone={restored or 1}&tab={form.get('tab', 'list')}",
        status_code=status.HTTP_303_SEE_OTHER)
