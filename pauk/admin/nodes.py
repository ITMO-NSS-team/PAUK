"""Node screens: search, one node's page, editing it, removing it.

Every write here repeats what `pauk admin node set|delete` does, in the
same order and for the same reason: the graph write goes first, because
it is the step that can be refused, and the decision is recorded only
once the write succeeded. A decision stored for an edit that never
happened would be applied by the next publish — quietly making a change
the person was just told was rejected.

Nothing in this module talks to the driver directly. Labels and field
names are interpolated into Cypher, so they can only come from the
whitelists in `pauk.graph.mutations`, never from the request.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from pauk.admin.deps import CsrfChecked, CurrentUser, Db, Editor, Graph, Session, templates
from pauk.graph.mutations import (
    NODE_FIELDS,
    RELATIONSHIPS,
    RESERVED_FIELDS,
    SEARCH_FIELDS,
    SEARCH_LIMIT,
    MutationError,
    NotFound,
    create_node,
    create_relationship,
    delete_node,
    delete_relationship,
    node_relationships,
    read_node,
    search_nodes,
    update_node,
)
from pauk.graph.overrides import apply_overrides, record_override, record_relationship_override

logger = logging.getLogger("pauk.admin")

router = APIRouter()


def _worded(relationships: list[dict], label: str) -> list[dict]:
    """Add the human phrase for each edge, read from this node's side."""
    for rel in relationships:
        other = rel["labels"][0]
        triple = (label, rel["type"], other) if rel["outgoing"] else (other, rel["type"], label)
        forward, backward = LINK_WORDS.get(triple, (rel["type"], rel["type"]))
        rel["words"] = forward if rel["outgoing"] else backward
    return relationships


def _known_label(label: str) -> str:
    """Reject an unknown label with 404 rather than let it reach Cypher."""
    if label not in NODE_FIELDS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown label: {label}")
    return label


def _parse_value(raw: str):
    """Turn a form field into what should be stored.

    An empty box means "clear this field", which is None rather than the
    empty string — the pipeline writes None for what it did not find, and
    a hand-cleared field should look the same to everything downstream.
    """
    text = raw.strip()
    return text or None


@router.get("/nodes/{label}", response_class=HTMLResponse)
def search(request: Request, label: str, user: CurrentUser, session: Session,
           graph: Graph, q: str = ""):
    _known_label(label)
    # Явно тем же числом, что уходит в шаблон: иначе подпись «это первые N»
    # сравнивает длину списка не с тем лимитом и никогда не показывается.
    rows = search_nodes(graph, label, q, SEARCH_LIMIT)
    return templates.TemplateResponse(request, "search.html", {
        "user": user, "csrf": session["csrf"], "label": label, "query": q,
        "rows": rows, "limit": SEARCH_LIMIT, "fields": SEARCH_FIELDS[label],
        "labels": sorted(NODE_FIELDS)})


@router.get("/nodes/{label}/new", response_class=HTMLResponse)
def create_form(request: Request, label: str, user: Editor, session: Session):
    """The form for a node the pipeline does not know about.

    Declared before the node page: otherwise `/nodes/Person/new` matches
    that route and goes looking for a node whose id is "new".
    """
    _known_label(label)
    return templates.TemplateResponse(request, "create.html", {
        "user": user, "csrf": session["csrf"], "label": label,
        "editable": sorted(NODE_FIELDS[label]), "labels": sorted(NODE_FIELDS)})


@router.post("/nodes/{label}/new")
async def create(request: Request, label: str, user: Editor,
                 db: Db, graph: Graph, _: CsrfChecked):
    """Add a node by hand.

    No override is recorded: the loader only touches ids it has rows for,
    so an id invented here is never overwritten and needs nothing to
    reapply. Compare a *changed* field on a node the pipeline does know,
    which publishing would undo without one.
    """
    _known_label(label)
    form = await request.form()
    node_id = str(form.get("id", "")).strip()
    if not node_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "the node needs an id")
    fields = {name: value for name in NODE_FIELDS[label]
              if (value := _parse_value(str(form.get(name, "")))) is not None}
    try:
        create_node(graph, label, node_id, fields)
    except MutationError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from None
    logger.info("%s created %s %s", user.actor, label, node_id)
    return RedirectResponse(f"/nodes/{label}/{node_id}?created=1",
                            status_code=status.HTTP_303_SEE_OTHER)


@router.get("/nodes/{label}/{node_id}", response_class=HTMLResponse)
def show(request: Request, label: str, node_id: str, user: CurrentUser,
         session: Session, graph: Graph):
    _known_label(label)
    try:
        props = read_node(graph, label, node_id)
    except NotFound as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from None
    editable = sorted(NODE_FIELDS[label])
    return templates.TemplateResponse(request, "node.html", {
        "user": user, "csrf": session["csrf"], "label": label, "node_id": node_id,
        "props": props, "editable": editable, "reserved": sorted(RESERVED_FIELDS),
        "relationships": _worded(node_relationships(graph, label, node_id), label),
        "links": _links_for(label), "labels": sorted(NODE_FIELDS)})


@router.post("/nodes/{label}/{node_id}")
async def edit(request: Request, label: str, node_id: str, user: Editor,
               db: Db, graph: Graph, _: CsrfChecked):
    """Change fields, and remember the decision so a publish cannot undo it."""
    _known_label(label)
    form = await request.form()
    fields = {name: _parse_value(str(form[name]))
              for name in NODE_FIELDS[label] if name in form}
    note = str(form.get("note", "")).strip()
    if not fields:
        return RedirectResponse(f"/nodes/{label}/{node_id}", status_code=status.HTTP_303_SEE_OTHER)

    try:
        before = read_node(graph, label, node_id)
        # Only what actually differs is written: submitting a form
        # unchanged must not stamp an override on every field of the node,
        # nor fill the audit feed with edits nobody made.
        changed = {name: value for name, value in fields.items() if before.get(name) != value}
        if not changed:
            return RedirectResponse(f"/nodes/{label}/{node_id}?unchanged=1",
                                    status_code=status.HTTP_303_SEE_OTHER)
        update_node(graph, label, node_id, changed)
        record_override(db, label, node_id, "set", changed, actor=user.actor, note=note,
                        auto_value={name: before.get(name) for name in changed})
    except MutationError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from None
    logger.info("%s edited %s %s: %s", user.actor, label, node_id, ", ".join(sorted(changed)))
    return RedirectResponse(f"/nodes/{label}/{node_id}?saved=1",
                            status_code=status.HTTP_303_SEE_OTHER)


@router.post("/nodes/{label}/{node_id}/delete")
async def remove(request: Request, label: str, node_id: str, user: Editor,
                 db: Db, graph: Graph, _: CsrfChecked):
    """Remove a node and tombstone it, so publishing does not bring it back."""
    _known_label(label)
    form = await request.form()
    cascade = bool(form.get("cascade"))
    try:
        # Same order as the edit: a node with relationships and no cascade
        # is refused, and a tombstone left behind would delete it on every
        # later run.
        delete_node(graph, label, node_id, cascade=cascade)
        record_override(db, label, node_id, "delete", actor=user.actor,
                        note=str(form.get("note", "")).strip())
        apply_overrides(graph, db)
    except MutationError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from None
    logger.info("%s deleted %s %s", user.actor, label, node_id)
    return RedirectResponse(f"/nodes/{label}?deleted={node_id}",
                            status_code=status.HTTP_303_SEE_OTHER)


def _links_for(label: str) -> dict[str, list[dict]]:
    """The relationships this label is allowed to have, split by direction.

    Read off `RELATIONSHIPS`, so the form can only ever offer one of the
    eleven triples the graph knows. The match property travels with each
    one: a Repository is matched by `url` and a GitHubProfile by `login`,
    not by an id, and the form has to say so or people will paste the
    wrong thing.
    """
    outgoing, incoming = [], []
    for (src_label, rel_type, tgt_label), match_prop in sorted(RELATIONSHIPS.items()):
        forward, backward = LINK_WORDS.get((src_label, rel_type, tgt_label), (rel_type, rel_type))
        entry = {"src_label": src_label, "rel_type": rel_type, "tgt_label": tgt_label,
                 "match_prop": match_prop, "forward": forward, "backward": backward,
                 "key": f"{src_label}|{rel_type}|{tgt_label}"}
        if src_label == label:
            outgoing.append(entry)
        if tgt_label == label:
            # Во входящей связи вводят источник, а источник загрузчик
            # адресует идентификатором — match_prop относится к цели,
            # которой здесь оказывается сам открытый узел.
            incoming.append({**entry, "match_prop": "id"})
    return {"outgoing": outgoing, "incoming": incoming}


def _triple(raw: str) -> tuple[str, str, str]:
    """Split the form's `Label|TYPE|Label` into a triple.

    Only the shape is checked here — that there are three parts at all.
    Whether the triple is one the graph allows is `validate_relationship`'s
    to decide, and it refuses before reaching the driver, listing every
    permitted triple in the message. Repeating that check would be a second
    copy of the whitelist to keep in step with the first.
    """
    parts = raw.split("|")
    if len(parts) != 3:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"expected Label|TYPE|Label, got {raw!r}")
    return parts[0], parts[1], parts[2]


# Как связь читается по-русски: сначала от того узла, из которого она
# исходит, потом от того, в который входит. Типы вроде MENTIONS_LINK или
# PRODUCED_BY человеку ничего не говорят, а решение "связать" принимают по
# смыслу, а не по названию ребра в графе.
LINK_WORDS = {
    ("Department", "PART_OF", "Department"): ("входит в подразделение", "включает подразделение"),
    ("Department", "PART_OF", "Organization"): ("входит в организацию", "включает подразделение"),
    ("Person", "AUTHORED", "Publication"): ("написал публикацию", "написана автором"),
    ("Person", "BELONGS_TO", "Department"): ("работает в подразделении", "здесь работает"),
    ("Person", "CONTRIBUTED_TO", "Repository"): ("участвовал в разработке", "в разработке участвовал"),
    ("Publication", "MENTIONS_LINK", "LinkCandidate"): ("ссылается на адрес", "упомянут в публикации"),
    ("Publication", "MENTIONS_LINK", "Repository"): ("ссылается на репозиторий", "упомянут в публикации"),
    ("Publication", "PRODUCED_BY", "Department"): ("сделана в подразделении", "здесь сделана публикация"),
    ("Repository", "DEVELOPED_BY", "Department"): ("разработан в подразделении", "здесь разработан репозиторий"),
    ("Repository", "IMPLEMENTS", "Publication"): ("реализует публикацию", "реализована в репозитории"),
    ("Repository", "OWNED_BY", "GitHubProfile"): ("принадлежит аккаунту", "владеет репозиторием"),
}

_FIELD_HINTS = {"url": "адрес репозитория целиком", "login": "логин аккаунта на GitHub"}


def _hint(match_prop: str) -> str:
    return _FIELD_HINTS.get(match_prop, f"значение поля {match_prop}")


def _link_failed(label: str, node_id: str, message: str) -> RedirectResponse:
    """Back to the node page with the reason, instead of a bare 400.

    A failed link is an ordinary mistake — a typo, the wrong field — and
    the person needs the form again, not a JSON error page.
    """
    return RedirectResponse(f"/nodes/{label}/{node_id}?error={quote(message, safe='')}",
                            status_code=status.HTTP_303_SEE_OTHER)


@router.post("/nodes/{label}/{node_id}/rel/add")
async def link(request: Request, label: str, node_id: str, user: Editor,
               db: Db, graph: Graph, _: CsrfChecked):
    """Connect this node to another one.

    No override is recorded, and that is not an omission: the loader only
    ever MERGEs the edges it has rows for and never removes the ones it
    does not know about, so a hand-made link survives publishing on its
    own. Recording one would be a decision nothing ever has to reapply.
    """
    _known_label(label)
    form = await request.form()
    src_label, rel_type, tgt_label = _triple(str(form.get("triple", "")))
    other = str(form.get("other_id", "")).strip()
    if not other:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "the other end is empty")

    # The node whose page this is sits on whichever end its label matches;
    # the person only ever types the other one.
    # Читается до создания, потому что от него зависит, какой стороной
    # подставить открытый узел. Отсутствие тройки здесь — не 500: форма
    # такого не пришлёт, но запрос мог прийти и мимо неё.
    match_prop = RELATIONSHIPS.get((src_label, rel_type, tgt_label))
    if match_prop is None:
        return _link_failed(label, node_id,
                            f"Граф не знает связи ({src_label})-[:{rel_type}]->({tgt_label}).")
    try:
        if src_label == label:
            src_id, tgt_id, other_label, wanted = node_id, other, tgt_label, match_prop
        else:
            # This node is the target, and the target is matched by
            # match_prop — its id is the wrong value to send when that is
            # something else, such as a repository's url.
            src_id, other_label, wanted = other, src_label, "id"
            tgt_id = node_id if match_prop == "id" else read_node(graph, label, node_id).get(match_prop)
            if not tgt_id:
                return _link_failed(
                    label, node_id,
                    f"У этого узла не заполнено поле {match_prop}, а связь ищет по нему. "
                    f"Заполните {match_prop} и повторите.")
        create_relationship(graph, src_label, rel_type, tgt_label, src_id, tgt_id)
    except NotFound:
        # The usual mistake is pasting an id where the link is matched by
        # something else — a Repository by its url, a GitHubProfile by its
        # login. Say which field this particular link needs.
        return _link_failed(
            label, node_id,
            f"{other_label} с {wanted} = «{other}» в графе нет. "
            f"Эта связь ищет вторую сторону по полю {wanted}"
            + (f", а не по идентификатору — впишите {_hint(wanted)}."
               if wanted != "id" else ". Проверьте идентификатор."))
    except MutationError as error:
        return _link_failed(label, node_id, str(error))
    logger.info("%s linked (%s %s)-[:%s]->(%s %s)",
                user.actor, src_label, src_id, rel_type, tgt_label, tgt_id)
    return RedirectResponse(f"/nodes/{label}/{node_id}?linked=1",
                            status_code=status.HTTP_303_SEE_OTHER)


@router.post("/nodes/{label}/{node_id}/rel/delete")
async def unlink(request: Request, label: str, node_id: str, user: Editor,
                 db: Db, graph: Graph, _: CsrfChecked):
    """Disconnect two nodes and remember it, so a publish cannot relink them.

    Here the override does matter: the edge comes from a prepared row, and
    `MERGE` would recreate it on the next publish. The loader skips the
    tombstoned ones before writing.
    """
    _known_label(label)
    form = await request.form()
    src_label, rel_type, tgt_label = _triple(str(form.get("triple", "")))
    other = str(form.get("other_id", "")).strip()
    src_id, tgt_id = (node_id, other) if src_label == label else (other, node_id)
    try:
        removed = delete_relationship(graph, src_label, rel_type, tgt_label, src_id, tgt_id)
        if not removed:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "there is no such link")
        record_relationship_override(db, src_label, rel_type, tgt_label, src_id, tgt_id,
                                     actor=user.actor, note=str(form.get("note", "")).strip())
    except MutationError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from None
    logger.info("%s unlinked (%s %s)-[:%s]->(%s %s)",
                user.actor, src_label, src_id, rel_type, tgt_label, tgt_id)
    return RedirectResponse(f"/nodes/{label}/{node_id}?unlinked=1",
                            status_code=status.HTTP_303_SEE_OTHER)
