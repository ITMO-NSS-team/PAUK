"""The queue of pairs the deduplicator could not settle, as a page.

Nothing here merges anything. An answer is written down and the rules read
it on their next run (see `pauk.storage.review`), which is the only order
that works: a merge cannot be undone, so the decision has to reach the
algorithm before it decides, not patch the result afterwards.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from pauk.admin.deps import (
    CsrfChecked,
    CurrentUser,
    Db,
    Editor,
    MaybeGraph,
    Session,
    StoresReady,
    templates,
)
from pauk.graph.mutations import MutationError, NotFound, merge_nodes, read_node
from pauk.pipeline.stages.dedup import merge_rank
from pauk.storage import review

logger = logging.getLogger("pauk.admin")

router = APIRouter()

PAGE = 50

#: The tabs, and what each one asks the store for. "pressing" is the day's
#: work, "open" is everything nobody has answered, and the other two are
#: there so nothing the page does is hidden from it.
TABS = {
    "pressing": {"pressing": True, "answered": False, "skipped": False},
    "open": {"answered": False, "skipped": False},
    "skipped": {"skipped": True, "answered": False},
    "answered": {"answered": True},
}

WORDS = {
    "only one person is ITMO-affiliated": "только один из двоих в ИТМО",
    "no shared coauthors": "нет общих соавторов",
    "single-token display name": "имя из одного слова",
    "name is given as initials": "имя дано инициалами",
    "identical name with nothing corroborating it": "одинаковое имя, ничем не подтверждено",
}


def _reason_words(reason: str) -> str:
    """A held_because line in the words the page uses for it.

    A group's reason is composed at the moment it is refused ("group spans
    2 distinct ORCID values"), so it cannot be looked up whole.
    """
    if reason in WORDS:
        return WORDS[reason]
    if reason.startswith("group spans"):
        parts = reason.split()
        return f"в группе {parts[2]} разных значения поля {parts[-2]}"
    return reason


def _shown(row: dict) -> dict:
    """One question as the page reads it."""
    evidence = row.get("evidence", {})
    return {
        "id": row["_id"],
        "kind": row["kind"],
        # A flag rather than the constant in the template: a page comparing
        # kind to a literal was already wrong once, and silently — it put
        # the "one person" button on a group, which the route then refused.
        "is_group": row["kind"] == review.GROUP,
        "members": row["members"],
        # Paired with their names, because a page listing bare OpenAlex ids
        # asks a question nobody can answer.
        "people": list(zip(row["members"], evidence.get("names") or [], strict=False)),
        "reasons": [_reason_words(reason) for reason in evidence.get("held_because", [])],
        "shared_coauthors": evidence.get("shared_coauthors"),
        "shared_departments": evidence.get("shared_departments"),
        "shared_fields": evidence.get("shared_fields") or [],
        "seen_at": row.get("seen_at"),
        "verdict": row.get("verdict"),
        "actor": row.get("actor"),
        "note": row.get("note"),
        "skipped_by": row.get("skipped_by"),
        "applied_at": row.get("applied_at"),
    }


@router.get("/review", response_class=HTMLResponse)
def queue(request: Request, user: CurrentUser, session: Session, db: Db,
          tab: str = "pressing", page: int = 1):
    """Questions the rules left open, the longest-waiting first.

    Readable by anyone who can sign in. Answering needs the editor role:
    the answer changes what the graph will look like after the next run.
    """
    if tab not in TABS:
        tab = "pressing"
    page = max(page, 1)
    total = review.count(db, **TABS[tab])
    return templates.TemplateResponse(request, "review.html", {
        "user": user, "csrf": session["csrf"], "tab": tab, "page": page,
        "pages": max((total + PAGE - 1) // PAGE, 1), "total": total,
        "rows": [_shown(row) for row in
                 review.questions(db, limit=PAGE, skip=(page - 1) * PAGE, **TABS[tab])],
        "counts": {name: review.count(db, **filters) for name, filters in TABS.items()},
    })


def _fold_now(graph, db, members: list[str], actor: str) -> str:
    """Fold a confirmed pair straight away, when there is anything to fold.

    A pair held by the collection stage names people who are prepared rows
    and nothing else: their group has not been published, so no node exists
    and the answer simply waits for one. A pair held by the graph-wide pass
    names two live nodes, and making somebody wait for the next run to see
    their own decision take effect would be for nothing.

    Returns:
        "merged", or "waiting" when the graph cannot do it now. Either way
        the decision is already stored and the next run applies it.
    """
    if graph is None:
        return "waiting"
    try:
        rows = {node_id: read_node(graph, "Person", node_id) for node_id in members}
    except NotFound:
        return "waiting"
    ranked = sorted(members, key=lambda node_id: merge_rank(
        len([edge for edge in graph.fetch_node_relationships("Person", node_id)
             if edge["type"] == "AUTHORED" and edge["outgoing"]]),
        rows[node_id].get("orcid"), node_id))
    canonical, duplicate = ranked[0], ranked[1]
    try:
        merge_nodes(graph, "Person", duplicate, canonical)
    except MutationError as error:
        # Not the caller's problem: the answer stands and the next dedup
        # will fold the pair with the rest.
        logger.warning("could not fold %s into %s now: %s", duplicate, canonical, error)
        return "waiting"
    review.mark_applied(db, review.PAIR, members)
    logger.info("%s folded %s into %s from the review queue", actor, duplicate, canonical)
    return "merged"


@router.post("/review/answer")
async def answer(request: Request, user: Editor, db: Db, graph: MaybeGraph,
                 _: CsrfChecked, __: StoresReady):
    """Write down what somebody decided about one question.

    The decision is stored first and folded second, never the other way
    round: stored, it survives anything that happens next and the rules
    apply it themselves. A fold that ran before the decision was written
    would be a merge nobody could explain and nobody could repeat.
    """
    form = await request.form()
    kind = str(form.get("kind", review.PAIR))
    members = [part for part in str(form.get("members", "")).split(",") if part]
    verdict = str(form.get("verdict", ""))
    tab = str(form.get("tab", "pressing"))
    try:
        if verdict == "skip":
            if not review.skip(db, kind, members, actor=user.actor):
                raise HTTPException(status.HTTP_404_NOT_FOUND, "такого вопроса нет")
        else:
            review.record_verdict(db, kind, members, verdict, actor=user.actor,
                                  note=str(form.get("note", "")).strip())
    except review.ReviewError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from None
    logger.info("%s answered %s %s: %s", user.actor, kind, members, verdict)
    done = ""
    if verdict == review.SAME and kind == review.PAIR:
        done = _fold_now(graph, db, members, user.actor)
    return RedirectResponse(f"/review?tab={tab}&done={done}" if done else f"/review?tab={tab}",
                            status_code=status.HTTP_303_SEE_OTHER)


@router.post("/review/withdraw")
async def withdraw(request: Request, user: Editor, db: Db, _: CsrfChecked, __: StoresReady):
    """Take an answer back, leaving the question in the queue.

    The question stays because a real run asked it. Deleting it would only
    mean the next run asks the same thing from nothing.
    """
    form = await request.form()
    kind = str(form.get("kind", review.PAIR))
    members = [part for part in str(form.get("members", "")).split(",") if part]
    try:
        dropped = review.withdraw(db, kind, members)
    except review.ReviewError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from None
    if not dropped:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "такого вопроса нет")
    logger.info("%s withdrew the answer about %s %s", user.actor, kind, members)
    return RedirectResponse(f"/review?tab={form.get('tab', 'answered')}",
                            status_code=status.HTTP_303_SEE_OTHER)
