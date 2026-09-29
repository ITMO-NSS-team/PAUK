"""The queue of pairs the deduplicator could not settle, as a page.

An answer is written down first and the rules read it on their next run
(see `pauk.storage.review`); when both records are already published, the
fold also happens straight away, so nobody waits a day to see their own
decision take effect. Undoing one is the other way round — the graph comes
apart first and the answer is rewritten only if it did (`split_back`).
"""

from __future__ import annotations

import logging
import re
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
    plural,
    templates,
)
from pauk.graph.mutations import MutationError, NotFound, merge_nodes, read_node
from pauk.graph.unmerge import NothingToRebuild, rebuildable, split_person
from pauk.pipeline.stages.dedup import merge_rank
from pauk.storage import review

logger = logging.getLogger("pauk.admin")

router = APIRouter()

PAGE = 50

#: The tabs, and what each one asks the store for.
TABS = {
    "pressing": {"pressing": True, "answered": False, "skipped": False},
    "open": {"answered": False, "skipped": False},
    "disputed": {"disputed": True},
    "skipped": {"skipped": True, "answered": False},
    "answered": {"answered": True},
}

#: Rules in the words the page uses.
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


#: Composed when the group is refused, so matched rather than looked up.
GROUP_SPANS = re.compile(r"group spans (\d+) distinct (.+) values")

#: Identity fields the page names differently from the code.
FIELD_WORDS = {"staff record": "«запись в каталоге»"}


def _reason_words(reason: str) -> str:
    """A held_because line in the words the page uses for it."""
    if reason in WORDS:
        return WORDS[reason]
    spans = GROUP_SPANS.fullmatch(reason)
    if spans:
        count, field = int(spans.group(1)), spans.group(2)
        values = plural(count, "разное значение", "разных значения", "разных значений")
        return f"в группе {count} {values} поля {FIELD_WORDS.get(field, field)}"
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
    # In a staff question only the person has a card to open.
    person = row.get("person") or evidence.get("person")
    shown = []
    for index, member in enumerate(row["members"]):
        # An answer given before the question has no names to pair with.
        name = names[index] if index < len(names) else None
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
        # Four kinds of question run one after another; the names do not say which.
        "asks": _asks(row),
        # A flag, not a literal in the template: that was silently wrong once.
        "is_group": row["kind"] == review.GROUP,
        "is_github": row["kind"] == review.GITHUB,
        "is_staff": row["kind"] == review.STAFF,
        "chosen": row.get("chosen"),
        "members": row["members"],
        # Bare OpenAlex ids ask a question nobody can answer.
        "people": _people(row, evidence),
        "url": evidence.get("url"),
        "signals": [SIGNALS.get(name, name) for name in evidence.get("signals") or []],
        "repos": evidence.get("repos") or [],
        # A degree is what tells two namesakes apart.
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


def _tab(value: object, default: str) -> str:
    """A tab a form sent back, if it is one.

    It goes straight into the address the form is sent back to, and a value
    carrying "&" or "#" would add to that address whatever it liked.
    """
    return value if value in TABS else default


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
        "rows": _splittable(db, [_shown(row) for row in
                                 review.questions(db, limit=PAGE, skip=(page - 1) * PAGE,
                                                  **TABS[tab])]),
        "counts": {name: review.count(db, **filters) for name, filters in TABS.items()},
    })


def _splittable(db, rows: list[dict]) -> list[dict]:
    """Say which folded pairs can still be taken apart.

    Asked once for the whole page: a fold is undone by rebuilding the
    record from its prepared row, and the only thing the page needs to know
    is whether both rows are still there.
    """
    wanted = {member for row in rows if row["applied_at"] for member in row["members"]}
    have = rebuildable(db, wanted) if wanted else set()
    for row in rows:
        row["can_split"] = bool(row["applied_at"]) and set(row["members"]) <= have
    return rows


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
        # The answer stands; the next dedup folds the pair with the rest.
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
    tab = _tab(form.get("tab"), "pressing")
    note = str(form.get("note", "")).strip()

    def back(problem: str = "", done: str = ""):
        """To the queue, with a word about what happened.

        A form somebody filled in wrongly sends them back to it, not to an
        error page: the checkboxes are three clicks to redo, and a dead end
        with a status code on it explains nothing.
        """
        query = f"?tab={tab}"
        if problem:
            query += f"&problem={quote(problem)}"
        if done:
            query += f"&done={done}"
        return RedirectResponse(f"/review{query}", status_code=status.HTTP_303_SEE_OTHER)

    try:
        if verdict == "skip":
            if not review.skip(db, kind, members, actor=user.actor):
                raise HTTPException(status.HTTP_404_NOT_FOUND, "такого вопроса нет")
        elif verdict == "choose":
            # Not a yes or no: the answer names a record. Empty means none fits.
            person = str(form.get("person", ""))
            if person not in members:
                # An empty person would build a key with a blank segment.
                raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                    "не указано, о ком вопрос")
            chosen = str(form.get("chosen", "")).strip()
            review.record_choice(db, person,
                                 [member for member in members if member != person],
                                 chosen or None, actor=user.actor, note=note)
            return back(done="chosen")
        elif verdict == "split":
            # The store guards this too; this is what a person reads.
            same = form.getlist("same")
            if len(same) < 2:
                return back("Отметьте хотя бы двоих, кого считаете одним человеком.")
            if len(same) >= len(members):
                return back("Вся группа не может быть одним человеком — её отклонили "
                            "как раз потому, что внутри разные люди.")
            # Nothing is folded here: later merges would name a swallowed node.
            review.record_split(db, members, same, actor=user.actor, note=note)
            return back(done="split")
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


@router.post("/review/split-back")
async def split_back(request: Request, user: Editor, db: Db, graph: MaybeGraph,
                     _: CsrfChecked, __: StoresReady):
    """Take a fold apart again: the record merged away comes back.

    The graph goes first here and the store second, the opposite of
    answering. An answer is worth keeping whatever happens next, because
    the rules apply it themselves; an undo is worth nothing until the graph
    actually comes apart, and "different" written over a fold that refused
    to open would describe a graph that does not exist.
    """
    form = await request.form()
    kind = str(form.get("kind", review.PAIR))
    members = [part for part in str(form.get("members", "")).split(",") if part]
    tab = _tab(form.get("tab"), "answered")

    def back(problem: str = "", done: str = ""):
        query = f"?tab={tab}"
        if problem:
            query += f"&problem={quote(problem)}"
        if done:
            query += f"&done={done}"
        return RedirectResponse(f"/review{query}", status_code=status.HTTP_303_SEE_OTHER)

    if kind != review.PAIR:
        # Only a pair is ever folded; the other kinds link rather than merge.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "слитой была только пара")
    if graph is None:
        return back("Граф недоступен, разделить записи сейчас нельзя.")
    # Before the graph is touched: an undo the store refuses would strand it.
    try:
        applied = review.applied(db, kind, members)
    except review.ReviewError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from None
    if not applied:
        return back("Это решение ничего не сливало, разделять нечего.")
    try:
        split_person(graph, db, members)
    except NothingToRebuild as error:
        logger.warning("%s could not split %s: %s", user.actor, members, error)
        return back("Вернуть запись нечем: подготовленных строк для неё не осталось "
                    "или её удалили вручную.")
    except NotFound as error:
        logger.warning("%s could not split %s: %s", user.actor, members, error)
        return back("В графе нет этого слияния. Возможно, записи уже разделили.")
    except MutationError as error:
        logger.warning("%s could not split %s: %s", user.actor, members, error)
        return back("Разделить не удалось, подробности в журнале сервиса.")
    # A verdict, not a withdrawal: "same" would fold the pair again.
    review.record_undo(db, kind, members, actor=user.actor)
    logger.info("%s split %s back apart", user.actor, members)
    return back(done="apart")


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
    return RedirectResponse(f"/review?tab={_tab(form.get('tab'), 'answered')}",
                            status_code=status.HTTP_303_SEE_OTHER)
