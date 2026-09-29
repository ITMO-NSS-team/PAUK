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

import json
import logging
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from neo4j.exceptions import Neo4jError
from pymongo.errors import PyMongoError

from pauk.admin import decisions, feed, source
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
    LIST_ORDER,
    NODE_FIELDS,
    RELATIONSHIPS,
    RESERVED_FIELDS,
    SEARCH_FIELDS,
    SEARCH_LIMIT,
    MutationError,
    NotFound,
    VersionConflict,
    columns,
    create_node,
    create_relationship,
    delete_node,
    delete_relationship,
    folded_into,
    node_relationships,
    read_node,
    search_nodes,
    update_node,
)
from pauk.graph.overrides import (
    CREATE,
    LINK,
    deactivate_override,
    record_override,
    record_relationship_override,
)

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


def _parse_new_value(raw: str):
    """A value for a node that does not exist yet.

    Nothing in the graph to take a type from, so JSON decides: 42 is a
    number, true is a boolean, ["a"] is a list, and anything JSON refuses
    is plain text. Same rule as `pauk admin node create --set`.
    """
    text = raw.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def _node_url(label: str, node_id: str, query: str = "") -> str:
    """The panel's address for one node.

    A LinkCandidate is identified by the address it was found at, so its
    id can hold "?" and "#" of its own. Left as they are, the browser
    reads them as the start of a query or a fragment and throws away the
    rest of the id. Slashes are left alone: the routes match the id with
    `{node_id:path}`, which takes them as part of it.
    """
    address = f"/nodes/{label}/{quote(node_id, safe='/')}"
    return f"{address}?{query}" if query else address


def _record(undo, write) -> None:
    """Write the decision that protects a change already made to the graph.

    Neo4j and Mongo are two databases with no transaction across them, so
    the decision can fail after the graph has already moved. Left there,
    the next publish takes the change back without a word: an edit reverts,
    a deleted record returns, a created one disappears.

    So the graph is put back instead. The undo lands in the audit feed
    beside the change, which is the only way anybody later sees what
    happened.

    Args:
        undo: Puts the graph back the way it was.
        write: Records the decision.

    Raises:
        HTTPException: 503. The wording says which of the two happened,
            because "saved" and "saved but unprotected" need different
            things from the person reading it.
    """
    try:
        write()
    except PyMongoError as error:
        logger.error("could not record the decision: %s", error)
        try:
            undo()
        except (MutationError, Neo4jError) as failure:
            logger.exception("and the graph could not be put back")
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "правка записана в граф, но не сохранилась как решение "
                f"и не откатилась ({failure}). Следующая публикация её снимет") from None
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "правка не сохранилась: Mongo не ответила, граф возвращён как был") from None


def _known_label(label: str) -> str:
    """Reject an unknown label with 404 rather than let it reach Cypher."""
    if label not in NODE_FIELDS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown label: {label}")
    return label


def _parse_value(raw: str, current: object = None):
    """Turn a form field into what should be stored.

    A browser submits every box on the form, including the ones nobody
    touched, and all of them arrive as text. Without a type to guide it,
    `stars_num` came back as "42" — different from 42, so it counted as an
    edit and was written to the graph as a string. The same held for
    booleans, years, counts and lists.

    The type comes from what the field already holds, which is what the
    pipeline put there. A field that is empty in the graph has nothing to
    go by and stays text; numbers and lists are not invented out of a
    string that merely looks like one.

    An empty box means "clear this field": None rather than "", because
    the pipeline writes None for what it did not find and a hand-cleared
    field should look the same to everything downstream.
    """
    text = raw.strip()
    if not text:
        return None
    if isinstance(current, bool):
        # Before int: a bool is an int in Python, and True would become 1.
        return text.lower() in ("true", "1", "да", "yes", "on")
    if isinstance(current, int):
        try:
            return int(text)
        except ValueError:
            return text
    if isinstance(current, float):
        try:
            return float(text)
        except ValueError:
            return text
    if isinstance(current, (list, dict)):
        try:
            parsed = json.loads(text)
        except ValueError:
            return text
        return parsed if isinstance(parsed, type(current)) else text
    return text


@router.get("/nodes/{label}", response_class=HTMLResponse)
def search(request: Request, label: str, user: CurrentUser, session: Session,
           graph: Graph, q: str = "", page: int = 1):
    """Nodes of one label, a page at a time.

    Whether there is a page after this one is asked as its own one-row
    question. The graph has no cheap count of what a search matches, and
    the "one row more than a page" trick does not work here: the page cap
    lives in the mutation layer and would clip the extra row away.
    """
    _known_label(label)
    page = max(page, 1)
    rows = search_nodes(graph, label, q, SEARCH_LIMIT, skip=(page - 1) * SEARCH_LIMIT)
    more = bool(rows) and bool(search_nodes(graph, label, q, 1, skip=page * SEARCH_LIMIT))
    return templates.TemplateResponse(request, "search.html", {
        "user": user, "csrf": session["csrf"], "label": label, "query": q,
        "rows": rows[:SEARCH_LIMIT], "limit": SEARCH_LIMIT, "page": page, "more": more,
        "fields": columns(label), "searched": SEARCH_FIELDS[label],
        "order": LIST_ORDER.get(label), "labels": sorted(NODE_FIELDS)})


@router.get("/nodes/{label}/new", response_class=HTMLResponse)
def create_form(request: Request, label: str, user: Editor, session: Session):
    """The form for a node the pipeline does not know about.

    Declared before the node page: otherwise `/nodes/Person/new` matches
    that route and goes looking for a node whose id is "new". The node
    routes take `{node_id:path}` because a LinkCandidate's id is a URL —
    slashes and all — and a plain segment would cut it into pieces and
    answer 404.
    """
    _known_label(label)
    return templates.TemplateResponse(request, "create.html", {
        "user": user, "csrf": session["csrf"], "label": label,
        "editable": sorted(NODE_FIELDS[label]), "labels": sorted(NODE_FIELDS)})


@router.post("/nodes/{label}/new")
async def create(request: Request, label: str, user: Editor,
                 db: Db, graph: Graph, _: CsrfChecked, __: StoresReady):
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
    # Such a node would list in the search and answer 404 on its own link.
    if any(character < " " or character == "\x7f" for character in node_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "the id cannot hold line breaks or control characters")
    # Nothing to compare against yet: text unless it parses as JSON, as in --set.
    fields = {name: value for name in NODE_FIELDS[label]
              if (value := _parse_new_value(str(form.get(name, "")))) is not None}
    try:
        create_node(graph, label, node_id, fields)
    except MutationError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from None
    # Claimed so a prune keeps it, and any earlier tombstone withdrawn.
    def claim() -> None:
        deactivate_override(db, label, node_id, only_op="delete")
        record_override(db, label, node_id, CREATE, fields, actor=user.actor)

    _record(undo=lambda: delete_node(graph, label, node_id), write=claim)
    logger.info("%s created %s %s", user.actor, label, node_id)
    return RedirectResponse(_node_url(label, node_id, "created=1"),
                            status_code=status.HTTP_303_SEE_OTHER)


@router.post("/nodes/{label}/restore/{node_id:path}")
async def restore(label: str, node_id: str, user: Editor,
                  db: Db, graph: Graph, _: CsrfChecked, __: StoresReady):
    """Put a deleted node back as it was, from the snapshot on its decision.

    A deletion records every field the node carried, so this is a real
    restore rather than an empty shell. The tombstone goes with it —
    otherwise the next publish would delete the node a second time.
    """
    _known_label(label)
    fields = decisions.deleted_fields(db, label, node_id)
    if not fields:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "не сохранилось, чем восстанавливать эту запись")
    try:
        create_node(graph, label, node_id,
                    {name: value for name, value in fields.items()
                     if name in NODE_FIELDS[label]})
    except MutationError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from None
    _record(undo=lambda: delete_node(graph, label, node_id),
            write=lambda: deactivate_override(db, label, node_id, only_op="delete"))
    logger.info("%s restored %s %s", user.actor, label, node_id)
    return RedirectResponse(_node_url(label, node_id, "restored=1"),
                            status_code=status.HTTP_303_SEE_OTHER)


@router.get("/nodes/{label}/{node_id:path}", response_class=HTMLResponse)
def show(request: Request, label: str, node_id: str, user: CurrentUser,
         session: Session, graph: Graph, db: Db, folded: str = ""):
    _known_label(label)
    try:
        props = read_node(graph, label, node_id)
    except NotFound:
        # A folded id has no node of its own; send the reader to the survivor.
        survivor = folded_into(graph, label, node_id)
        if survivor:
            # An id can be a URL of its own: escape it whole for the query.
            return RedirectResponse(
                _node_url(label, survivor, f"folded={quote(node_id, safe='')}"),
                status_code=status.HTTP_303_SEE_OTHER)
        # Feed links outlive their nodes: answer "what happened to it", not 404.
        gone = feed.history(db, label, node_id, limit=20)
        # Restoring reads the snapshot on the decision, not the feed.
        restorable = decisions.deleted_fields(db, label, node_id)
        return templates.TemplateResponse(request, "gone.html", {
            "user": user, "csrf": session["csrf"], "label": label, "node_id": node_id,
            "history": gone, "restorable": restorable,
            "labels": sorted(NODE_FIELDS)},
            status_code=status.HTTP_404_NOT_FOUND)
    editable = sorted(NODE_FIELDS[label])
    return templates.TemplateResponse(request, "node.html", {
        "user": user, "csrf": session["csrf"], "label": label, "node_id": node_id,
        "props": props, "editable": editable, "reserved": sorted(RESERVED_FIELDS),
        # Only an id this record really swallowed: the value comes from the URL.
        "folded": folded if folded in (props.get("merged_ids") or []) else "",
        "relationships": _worded(node_relationships(graph, label, node_id), label),
        "history": feed.history(db, label, node_id, limit=10),
        "source_history": source.history(db, label, node_id),
        "source_versions": source.count(db, label, node_id),
        "links": _links_for(label), "labels": sorted(NODE_FIELDS)})


@router.post("/nodes/{label}/delete/{node_id:path}")
async def remove(request: Request, label: str, node_id: str, user: Editor,
                 db: Db, graph: Graph, _: CsrfChecked, __: StoresReady):
    """Remove a node and tombstone it, so publishing does not bring it back."""
    _known_label(label)
    form = await request.form()
    cascade = bool(form.get("cascade"))
    try:
        # Snapshot first: after the delete there is nothing left to read.
        snapshot = read_node(graph, label, node_id)
        kept = {name: value for name, value in snapshot.items()
                if name in NODE_FIELDS[label] and value is not None}
        delete_node(graph, label, node_id, cascade=cascade)
        # Without the tombstone the next publish brings the record back.
        _record(undo=lambda: create_node(graph, label, node_id, kept),
                write=lambda: record_override(
                    db, label, node_id, "delete", actor=user.actor,
                    note=str(form.get("note", "")).strip(), snapshot=kept))
    except MutationError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from None
    logger.info("%s deleted %s %s", user.actor, label, node_id)
    return RedirectResponse(f"/nodes/{label}?deleted={quote(node_id, safe='')}",
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
            # The source is what gets typed here, and it is addressed by id.
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


# How each link reads in Russian: leaving the node, then entering it.
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
    return RedirectResponse(_node_url(label, node_id, f"error={quote(message, safe='')}"),
                            status_code=status.HTTP_303_SEE_OTHER)


@router.post("/nodes/{label}/rel/add/{node_id:path}")
async def link(request: Request, label: str, node_id: str, user: Editor,
               db: Db, graph: Graph, _: CsrfChecked, __: StoresReady):
    """Connect this node to another one.

    Recorded as a decision, though nothing ever reapplies it: publishing
    leaves an edge it has no row for alone, so the link survives on its own.
    What it does not survive is a prune, which has no other way of telling
    an edge a person added from one the pipeline has stopped making.
    """
    _known_label(label)
    form = await request.form()
    src_label, rel_type, tgt_label = _triple(str(form.get("triple", "")))
    other = str(form.get("other_id", "")).strip()
    if not other:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "the other end is empty")

    # This node takes whichever end its label matches; the other one is typed.
    match_prop = RELATIONSHIPS.get((src_label, rel_type, tgt_label))
    if match_prop is None:
        return _link_failed(label, node_id,
                            f"Граф не знает связи ({src_label})-[:{rel_type}]->({tgt_label}).")
    try:
        if src_label == label:
            src_id, tgt_id, other_label, wanted = node_id, other, tgt_label, match_prop
        else:
            # This node is the target, and a target is matched by match_prop.
            src_id, other_label, wanted = other, src_label, "id"
            tgt_id = node_id if match_prop == "id" else read_node(graph, label, node_id).get(match_prop)
            if not tgt_id:
                return _link_failed(
                    label, node_id,
                    f"У этого узла не заполнено поле {match_prop}, а связь ищет по нему. "
                    f"Заполните {match_prop} и повторите.")
        create_relationship(graph, src_label, rel_type, tgt_label, src_id, tgt_id)
    except NotFound:
        # Usually an id pasted where the link matches on a url or a login.
        return _link_failed(
            label, node_id,
            f"{other_label} с {wanted} = «{other}» в графе нет. "
            f"Эта связь ищет вторую сторону по полю {wanted}"
            + (f", а не по идентификатору — впишите {_hint(wanted)}."
               if wanted != "id" else ". Проверьте идентификатор."))
    except MutationError as error:
        return _link_failed(label, node_id, str(error))
    # Claimed so a prune tells it from an edge the pipeline stopped making.
    _record(undo=lambda: delete_relationship(graph, src_label, rel_type, tgt_label,
                                             src_id, tgt_id),
            write=lambda: record_relationship_override(
                db, src_label, rel_type, tgt_label, src_id, tgt_id,
                op=LINK, actor=user.actor))
    logger.info("%s linked (%s %s)-[:%s]->(%s %s)",
                user.actor, src_label, src_id, rel_type, tgt_label, tgt_id)
    return RedirectResponse(_node_url(label, node_id, "linked=1"),
                            status_code=status.HTTP_303_SEE_OTHER)


def _self_match_value(graph, label: str, node_id: str, match_prop: str) -> str:
    """This node's own value for the property an incoming edge is stored against.

    Raises:
        HTTPException: 400 when the field is empty. The edge cannot be
            named without it, and saying so beats removing nothing and
            reporting success.
    """
    value = read_node(graph, label, node_id).get(match_prop)
    if not value:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"у этого узла не заполнено поле {match_prop}, а связь хранится по нему")
    return str(value)


@router.post("/nodes/{label}/rel/delete/{node_id:path}")
async def unlink(request: Request, label: str, node_id: str, user: Editor,
                 db: Db, graph: Graph, _: CsrfChecked, __: StoresReady):
    """Disconnect two nodes and remember it, so a publish cannot relink them.

    Here the override does matter: the edge comes from a prepared row, and
    `MERGE` would recreate it on the next publish. The loader skips the
    tombstoned ones before writing.
    """
    _known_label(label)
    form = await request.form()
    src_label, rel_type, tgt_label = _triple(str(form.get("triple", "")))
    other = str(form.get("other_id", "")).strip()
    # As in the link form: a target is addressed by match_prop, not always by id.
    match_prop = RELATIONSHIPS.get((src_label, rel_type, tgt_label), "id")
    if src_label == label:
        src_id, tgt_id = node_id, other
    else:
        src_id = other
        tgt_id = node_id if match_prop == "id" else _self_match_value(graph, label, node_id, match_prop)
    try:
        removed = delete_relationship(graph, src_label, rel_type, tgt_label, src_id, tgt_id)
        if not removed:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "there is no such link")
        _record(
            undo=lambda: create_relationship(graph, src_label, rel_type, tgt_label,
                                             src_id, tgt_id),
            write=lambda: record_relationship_override(
                db, src_label, rel_type, tgt_label, src_id, tgt_id,
                actor=user.actor, note=str(form.get("note", "")).strip()))
    except MutationError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from None
    logger.info("%s unlinked (%s %s)-[:%s]->(%s %s)",
                user.actor, src_label, src_id, rel_type, tgt_label, tgt_id)
    return RedirectResponse(_node_url(label, node_id, "unlinked=1"),
                            status_code=status.HTTP_303_SEE_OTHER)


# Last on purpose: {node_id:path} is greedy and would swallow the routes above.
@router.post("/nodes/{label}/{node_id:path}")
async def edit(request: Request, label: str, node_id: str, user: Editor,
               db: Db, graph: Graph, _: CsrfChecked, __: StoresReady):
    """Change fields, and remember the decision so a publish cannot undo it."""
    _known_label(label)
    form = await request.form()
    note = str(form.get("note", "")).strip()
    try:
        # Inside the try: the record can be deleted while the form is open.
        before = read_node(graph, label, node_id)
        fields = {name: _parse_value(str(form[name]), before.get(name))
                  for name in NODE_FIELDS[label] if name in form}
        if not fields:
            return RedirectResponse(_node_url(label, node_id),
                                    status_code=status.HTTP_303_SEE_OTHER)
        # Only what differs: an unchanged form must not stamp every field.
        changed = {name: value for name, value in fields.items() if before.get(name) != value}
        if not changed:
            return RedirectResponse(_node_url(label, node_id, "unchanged=1"),
                                    status_code=status.HTTP_303_SEE_OTHER)
        # The updated_at the form was rendered with: parallel saves collide.
        update_node(graph, label, node_id, changed,
                    expected_updated_at=str(form.get("seen_at") or "") or None)
        _record(
            undo=lambda: update_node(graph, label, node_id,
                                     {name: before.get(name) for name in changed}),
            write=lambda: record_override(
                db, label, node_id, "set", changed, actor=user.actor, note=note,
                auto_value={name: before.get(name) for name in changed}))
    except VersionConflict:
        # Someone got there first: hand the page back with what is there now.
        logger.info("%s hit a version conflict on %s %s", user.actor, label, node_id)
        return RedirectResponse(_node_url(label, node_id, "stale=1"),
                                status_code=status.HTTP_303_SEE_OTHER)
    except NotFound:
        # Deleted while the form was open; its own page explains that.
        logger.info("%s saved %s %s after it was deleted", user.actor, label, node_id)
        return RedirectResponse(_node_url(label, node_id),
                                status_code=status.HTTP_303_SEE_OTHER)
    except MutationError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from None
    logger.info("%s edited %s %s: %s", user.actor, label, node_id, ", ".join(sorted(changed)))
    return RedirectResponse(_node_url(label, node_id, "saved=1"),
                            status_code=status.HTTP_303_SEE_OTHER)
