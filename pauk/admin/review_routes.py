"""The queue of pairs the deduplicator could not settle, as a page.

Nothing here merges anything. An answer is written down and the rules read
it on their next run (see `pauk.storage.review`), which is the only order
that works: a merge cannot be undone, so the decision has to reach the
algorithm before it decides, not patch the result afterwards.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

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
    "disputed": {"disputed": True},
    "skipped": {"skipped": True, "answered": False},
    "answered": {"answered": True},
}

#: Rules in the words the page uses. A rule name is written for the code
#: that applies it, not for the person reading why it fired.
RULES = {
    "orcid": "совпал ORCID",
    "staff_catalog": "одна запись в каталоге сотрудников",
    "name_variant": "имя одного значится вариантом имени другого",
    "same_name": "одинаковое имя, и есть чем подтвердить",
    "manual": "решение человека",
}

WORDS = {
    "only one person is ITMO-affiliated": "только один из двоих в ИТМО",
    "no shared coauthors": "нет общих соавторов",
    "single-token display name": "имя из одного слова",
    "name is given as initials": "имя дано инициалами",
    "identical name with nothing corroborating it": "одинаковое имя, ничем не подтверждено",
    "the name matches exactly and nothing else backs it":
        "имя совпадает целиком, больше ничего не подтверждает",
    "a similar name and a shared publication, nothing more":
        "похожее имя и общая публикация, больше ничего",
    "the catalog holds several people under this name":
        "каталог знает нескольких человек с таким именем",
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


#: What each github signal says, for the column that explains a match.
SIGNALS = {
    "email_exact": "тот же адрес",
    "name_exact": "имя совпадает целиком",
    "name_fuzzy": "имя похоже",
    "itmo_email": "адрес в домене ИТМО",
    "login_surname": "фамилия в логине",
    "owner": "владелец репозитория",
    "org_itmo": "состоит в организации ИТМО",
    "itmo_profile": "ИТМО указан в профиле",
}


def _asks(row: dict) -> str:
    """What this row is a question about, in two or three words."""
    kind = row["kind"]
    if kind == review.GROUP:
        return f"группа из {len(row['members'])}"
    if kind == review.GITHUB:
        return "аккаунт GitHub"
    if kind == review.STAFF:
        return "запись каталога"
    return "две записи"


def _people(row: dict, evidence: dict) -> list[dict]:
    """The subjects of one question, each with somewhere to look.

    A person is a node the panel can open. An account is not: it lives on
    GitHub, and the only useful thing to do with it is go and look.
    """
    names = evidence.get("names") or []
    login = evidence.get("login")
    # A staff question is about one person and the records they might be;
    # only the person has a card to open.
    person = row.get("person") or evidence.get("person")
    shown = []
    for member, name in zip(row["members"], names, strict=False):
        account = row["kind"] == review.GITHUB and member == login
        record = row["kind"] == review.STAFF and member != person
        shown.append({
            "id": member,
            "name": name or member,
            "href": None if record else
                    (evidence.get("url") if account else f"/nodes/Person/{quote(member)}"),
            "account": account,
            "record": record,
        })
    return shown


def _shown(row: dict) -> dict:
    """One question as the page reads it."""
    evidence = row.get("evidence", {})
    return {
        "id": row["_id"],
        "kind": row["kind"],
        # A flag rather than the constant in the template: a page comparing
        # kind to a literal was already wrong once, and silently — it put
        # the "one person" button on a group, which the route then refused.
        # Названо в строке, а не выводится из набора кнопок: в таблице
        # четыре разных вопроса подряд, и по подписям под именами не
        # понять, про что этот.
        "asks": _asks(row),
        "is_group": row["kind"] == review.GROUP,
        "is_github": row["kind"] == review.GITHUB,
        "is_staff": row["kind"] == review.STAFF,
        "chosen": row.get("chosen"),
        "members": row["members"],
        # Paired with their names and with somewhere to look, because a page
        # listing bare OpenAlex ids asks a question nobody can answer. An
        # account is not a node the panel can open, so it points at GitHub.
        "people": _people(row, evidence),
        "url": evidence.get("url"),
        "signals": [SIGNALS.get(name, name) for name in evidence.get("signals") or []],
        "repos": evidence.get("repos") or [],
        # A degree is what tells two namesakes apart when the catalog has
        # one; collected already, and useless sitting in the document.
        "degrees": dict(zip(evidence.get("records") or [],
                            evidence.get("record_degrees") or [], strict=False)),
        "reasons": [_reason_words(reason) for reason in evidence.get("held_because", [])],
        "shared_coauthors": evidence.get("shared_coauthors"),
        "shared_departments": evidence.get("shared_departments"),
        "shared_fields": evidence.get("shared_fields") or [],
        "seen_at": row.get("seen_at"),
        "verdict": row.get("verdict"),
        "actor": row.get("actor"),
        "note": row.get("note"),
        "person": row.get("person") or row.get("evidence", {}).get("person"),
        "skipped_by": row.get("skipped_by"),
        "applied_at": row.get("applied_at"),
        "disputed_at": row.get("disputed_at"),
        "disputed_rule": RULES.get(row.get("disputed_rule"), row.get("disputed_rule")),
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
    note = str(form.get("note", "")).strip()
    try:
        if verdict == "skip":
            if not review.skip(db, kind, members, actor=user.actor):
                raise HTTPException(status.HTTP_404_NOT_FOUND, "такого вопроса нет")
        elif verdict == "choose":
            # Not a yes or no: two namesakes are both plausible and exactly
            # one is right, so the answer names a record instead of taking
            # a side. An empty choice means the catalog does not hold them.
            person = str(form.get("person", ""))
            if person not in members:
                # Without this an empty or stray person builds a key with a
                # blank segment, and the answer describes nobody.
                raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                    "не указано, о ком вопрос")
            chosen = str(form.get("chosen", "")).strip()
            review.record_choice(db, person,
                                 [member for member in members if member != person],
                                 chosen or None, actor=user.actor, note=note)
            return RedirectResponse(f"/review?tab={tab}&done=chosen",
                                    status_code=status.HTTP_303_SEE_OTHER)
        elif verdict == "split":
            # Nothing is folded here even when the nodes exist: a split is
            # several merges, and the later ones would point at a node the
            # earlier ones had already swallowed.
            review.record_split(db, members, form.getlist("same"),
                                actor=user.actor, note=note)
            return RedirectResponse(f"/review?tab={tab}&done=split",
                                    status_code=status.HTTP_303_SEE_OTHER)
        else:
            review.record_verdict(db, kind, members, verdict, actor=user.actor, note=note)
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
