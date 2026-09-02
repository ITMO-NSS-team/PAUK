"""Stage 4 — LLM adjudication of the `llm` band.

Only pairs that stages 0-2 could neither accept nor reject reach here. The
model is given the two names plus graph context (kind, parent, shared staff
and publications, whether the names co-occur in one affiliation string) and
returns one of:

    same          -> merge
    parent_child  -> not a merge; recorded as a PART_OF suggestion in the journal
    sibling       -> hold
    unrelated     -> hold
    unknown       -> hold (the call failed or the reply did not parse)

Verdicts are cached in Mongo keyed by the normalized name pair + model +
prompt version, so a re-run is free and byte-for-byte reproducible and the
model is only asked about pairs it has not seen.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from pymongo.database import Database

from pauk.sources import OpenRouterClient
from pauk.storage import LlmLogStore

from .matching import DepartmentRecord, PairSignals
from .normalize import normalize

logger = logging.getLogger(__name__)

PROMPT_VERSION = "2026-09-02"
VERDICT_COLLECTION = "dept_dedup_verdicts"
LLM_LOG_COLLECTION = "llm_logs_dept_dedup"

_RELATIONS = ("same", "parent_child", "sibling", "unrelated")

PROMPT_TEMPLATE = """You are reconciling organizational units of ITMO University. Given two unit \
name strings and context from a citation graph, decide their relationship.

Return ONLY a JSON object:
{{"relation": "same" | "parent_child" | "sibling" | "unrelated", "confidence": <0..1>, \
"reason": "<one sentence>"}}

relation:
  same         - two spellings / translations / former names of ONE unit
  parent_child - one unit is administratively part of the other
  sibling      - distinct units under a common parent
  unrelated    - distinct units, no direct link

Rules:
  - A RU<->EN translation or a rename over time is still "same".
  - A different domain word ("in Chemistry" vs "in Agrobiotechnology",
    "Technosphere" vs "Technogenic", "... Devices") means NOT "same".
  - "Laboratory of X" vs "Laboratory of X Assembly / Methods / Devices" is
    usually "parent_child".
  - If the two names co-occur in one affiliation string, they are almost
    never "same".
  - Only answer "same" with confidence >= 0.8 when you are sure; otherwise
    prefer "sibling" / "unrelated".

--- Unit A ---
name_en: {a_name_en}
name_ru: {a_name_ru}
kind:    {a_kind}
parent:  {a_parent}

--- Unit B ---
name_en: {b_name_en}
name_ru: {b_name_ru}
kind:    {b_kind}
parent:  {b_parent}

--- Graph context ---
authors linked:  A={a_staff}  B={b_staff}  shared={shared_staff}
publications:    A={a_pubs}   B={b_pubs}   shared={shared_pubs}
same parent:     {same_parent}
lexical token_set_ratio: {token_set:.2f}   embedding cosine: {embedding:.2f}
"""


@dataclass(frozen=True)
class Verdict:
    relation: str
    confidence: float
    reason: str

    @property
    def is_merge(self) -> bool:
        return self.relation == "same" and self.confidence >= 0.8


_UNKNOWN = Verdict("unknown", 0.0, "")


def _pair_key(a: DepartmentRecord, b: DepartmentRecord, model: str) -> str:
    left, right = sorted((normalize(a.names[0]).text, normalize(b.names[0]).text))
    raw = f"{PROMPT_VERSION}|{model}|{left}|{right}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def build_prompt(a: DepartmentRecord, b: DepartmentRecord, sig: PairSignals) -> str:
    return PROMPT_TEMPLATE.format(
        a_name_en=a.name_en or "-", a_name_ru=a.name_ru or "-",
        a_kind=a.kind or "-", a_parent=a.parent_id or "-",
        b_name_en=b.name_en or "-", b_name_ru=b.name_ru or "-",
        b_kind=b.kind or "-", b_parent=b.parent_id or "-",
        a_staff=len(a.staff_ids), b_staff=len(b.staff_ids),
        shared_staff=len(a.staff_ids & b.staff_ids),
        a_pubs=len(a.publication_ids), b_pubs=len(b.publication_ids),
        shared_pubs=len(a.publication_ids & b.publication_ids),
        same_parent="yes" if a.parent_id and a.parent_id == b.parent_id else "no",
        token_set=sig.token_set, embedding=sig.embedding_cosine,
    )


def _parse(reply: dict | None) -> Verdict:
    if not reply:
        return _UNKNOWN
    relation = str(reply.get("relation", "")).strip().lower()
    if relation not in _RELATIONS:
        return _UNKNOWN
    try:
        confidence = max(0.0, min(1.0, float(reply.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    return Verdict(relation, confidence, str(reply.get("reason", "")).strip()[:300])


class Adjudicator:
    """LLM verdicts for department pairs, with a persistent cache."""

    def __init__(self, db: Database, client: OpenRouterClient) -> None:
        self._verdicts = db[VERDICT_COLLECTION]
        self._log = LlmLogStore(db, LLM_LOG_COLLECTION)
        self._client = client
        self._model = client.model
        self.calls = 0
        self.cache_hits = 0

    def verdict(self, a: DepartmentRecord, b: DepartmentRecord, sig: PairSignals) -> Verdict:
        key = _pair_key(a, b, self._model)
        cached = self._verdicts.find_one({"_id": key})
        if cached:
            self.cache_hits += 1
            return Verdict(cached["relation"], cached["confidence"], cached.get("reason", ""))

        prompt = build_prompt(a, b, sig)
        reply = self._client.chat_json(prompt)
        self.calls += 1
        verdict = _parse(reply)
        self._log.record(
            group="graph", model=self._model, prompt=prompt,
            raw_response=self._client.last_response, parsed=reply,
            usage=self._client.last_usage, error=self._client.last_error,
            context={"pair": [a.id, b.id], "relation": verdict.relation},
        )
        if verdict.relation != "unknown":
            self._verdicts.update_one(
                {"_id": key},
                {"$set": {
                    "relation": verdict.relation, "confidence": verdict.confidence,
                    "reason": verdict.reason, "pair": sorted((a.id, b.id)),
                    "model": self._model, "prompt_version": PROMPT_VERSION,
                    "decided_at": datetime.now(UTC).isoformat(),
                }},
                upsert=True,
            )
        return verdict
