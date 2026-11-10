"""LLM verdict for a MEDIUM_FUZZY publication pair: one work or two.

Only the part of MEDIUM_FUZZY the rules leave open reaches the model (see
`version_candidates.route_medium_fuzzy`), and the verdict only sorts the
review queue: every pair still waits for a person.

A verdict once given is kept in `VERDICTS_COLLECTION` and reused rather than
asked again, so a re-run costs nothing unless it is forced. Every verdict is
kept, a "different" included: a pair routed out of the mergeable buckets is
not in the review queue, and a run without the LLM must not route it back.

The model is the project's standard one (`Settings.llm_model`), like every
other LLM stage. The routing was measured on 172 hand-labelled pairs with
qwen3-next: 0.96 precision against 0.76 for claude-haiku-4.5, whose extra
recall was almost all "one result at two conferences", which the policy
counts as two works. Another model routes differently; check its split on
the copy before trusting it.
"""

from __future__ import annotations

import logging

from pauk.sources import OpenRouterClient
from pauk.storage import LlmLogStore

logger = logging.getLogger(__name__)

LOG_COLLECTION = "llm_logs_version_candidates"
VERDICTS_COLLECTION = "version_llm_verdicts"

_MAX_AUTHORS = 8
_MAX_ABSTRACT = 1200

PROMPT_TEMPLATE = """Ты сравниваешь две записи научных публикаций из OpenAlex.

Запись A:
{a}

Запись B:
{b}

Вопрос: это одна и та же работа или две разные публикации?

Одна работа:
- препринт и его журнальная публикация;
- перевод статьи на другой язык;
- две записи одной статьи под разными DOI, ревизия препринта;
- доклад или тезисы на конференции и журнальная статья, в которой тот же
  объект, метод и главный результат (журнальная версия может быть шире).

Разные работы:
- два доклада на разных конференциях, даже с одним и тем же результатом;
- продолжение или развитие работы, новый образец, другой вариант метода;
- другое соединение, система или задача при похожем заголовке;
- обложка выпуска, заметка издателя, исправление.

Ответь СТРОГО валидным JSON без markdown:
{{"same_work": false, "reason": "одно короткое предложение"}}
"""


def _describe(row: dict) -> str:
    authors = [author.get("name") or author.get("person_id") for author in row.get("authors") or [] if author]
    abstract = " ".join((row.get("abstract") or "").split())
    if len(abstract) > _MAX_ABSTRACT:
        abstract = abstract[:_MAX_ABSTRACT] + "…"
    more = len(authors) - _MAX_AUTHORS
    return "\n".join([
        f"  Название: {row.get('title') or '(без названия)'}",
        f"  Площадка: {row.get('journal') or '(неизвестно)'} ({row.get('venue_type') or 'тип неизвестен'})",
        f"  Тип: {row.get('type') or '(неизвестно)'}",
        f"  Дата: {row.get('publication_date') or row.get('year') or '(неизвестно)'}",
        f"  DOI: {row.get('doi') or '(нет)'}",
        "  Авторы: " + (", ".join(authors[:_MAX_AUTHORS]) + (f" и ещё {more}" if more > 0 else "")
                        if authors else "(неизвестно)"),
        f"  Аннотация: {abstract or '(нет)'}",
    ])


def build_prompt(a: dict, b: dict) -> str:
    return PROMPT_TEMPLATE.format(a=_describe(a), b=_describe(b))


def _pair_key(a_id: str, b_id: str) -> str:
    return ":".join(sorted((a_id, b_id)))


def stored_verdicts(db, aliases: dict[str, str] | None = None) -> dict[frozenset[str], dict]:
    """Every verdict an earlier run paid for, keyed as `VersionJudge` looks it up.

    Args:
        aliases: Merged-away id to the id that survived, as for
            `review.version_decisions`. A publication folded since keeps its
            verdicts under the survivor, or the pair would be asked (and paid
            for) again.
    """
    aliases = aliases or {}
    found: dict[frozenset[str], dict] = {}
    for row in db[VERDICTS_COLLECTION].find():
        members = frozenset(aliases.get(member, member) for member in row["members"])
        if len(members) == 2:
            found[members] = {"same_work": row["same_work"], "reason": row.get("reason")}
    return found


def parse_verdict(result: dict) -> dict:
    """{"same_work": bool, "reason": str | None} or ValueError."""
    same_work = result.get("same_work")
    if same_work is not True and same_work is not False:
        raise ValueError("same_work must be true or false")
    reason = result.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise ValueError("reason must be a string or null")
    return {"same_work": same_work, "reason": (reason or "").strip() or None}


class VersionJudge:
    """Callable (a, b) -> verdict or None, as `route_medium_fuzzy` expects.

    Args:
        known: Verdicts already given, keyed by the frozenset of the two ids
            (`stored_verdicts`).
        client: Asked about pairs `known` has no verdict for. None answers
            from `known` alone, so a run without the LLM keeps the routing
            an earlier run paid for.
        log: Where every call is written in full.
        store: Mongo database to keep each new verdict in, under
            `VERDICTS_COLLECTION`; None keeps it in memory only.
        force: Ask the model even when `known` has the answer.
    """

    def __init__(self, known: dict[frozenset[str], dict] | None = None,
                 client: OpenRouterClient | None = None, log: LlmLogStore | None = None,
                 force: bool = False, store=None) -> None:
        self.known = known or {}
        self.client = client
        self.log = log
        self.force = force
        self.store = store
        self.asked = 0
        self.failed = 0

    def __call__(self, a: dict, b: dict) -> dict | None:
        key = frozenset((a["id"], b["id"]))
        if key in self.known and not (self.force and self.client is not None):
            return self.known[key]
        if self.client is None:
            return None
        prompt = build_prompt(a, b)
        result = self.client.chat_json(prompt)
        self.asked += 1
        verdict, error = None, None
        if result is None:
            error = self.client.last_error if isinstance(self.client.last_error, str) else "no response"
        else:
            try:
                verdict = parse_verdict(result)
            except ValueError as exc:
                error = str(exc)
        if self.log is not None:
            self.log.record(
                group="graph", model=self.client.model, prompt=prompt,
                raw_response=self.client.last_response, parsed=result,
                usage=self.client.last_usage, error=error,
                context={"record_a": a["id"], "record_b": b["id"]},
            )
        if verdict is None:
            self.failed += 1
            logger.warning("versions: no verdict for %s / %s: %s", a["id"], b["id"], error)
            # A forced re-ask that failed keeps the answer it meant to replace:
            # an outage must not put routed pairs back into the queue.
            return self.known.get(key)
        self.known[key] = verdict
        if self.store is not None:
            self.store[VERDICTS_COLLECTION].replace_one(
                {"_id": _pair_key(a["id"], b["id"])},
                {"members": sorted((a["id"], b["id"])), **verdict, "model": self.client.model},
                upsert=True)
        return verdict
