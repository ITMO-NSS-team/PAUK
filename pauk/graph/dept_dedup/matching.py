"""Stages 1-2 — candidate blocking and deterministic pair signals.

Stage 1 (`block`) turns all department records into candidate id pairs so the
LLM is only asked about a small, plausible subset. Stage 2 (`score_pair`)
computes lexical, token, acronym, semantic and graph-context signals for one
pair, and `assign_band` sorts it into:

    auto-merge   apply without asking (high-precision rules only)
    auto-reject  drop without asking
    llm          send to adjudication

The thresholds are deliberately conservative on the auto-merge side: a wrong
auto-merge is a silent bad fold, a wrong auto-reject just means the pair waits
for the next tightening of the rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from itertools import combinations

from .normalize import NormName, normalize

# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DepartmentRecord:
    """One Department node with everything the merge rules read."""

    id: str
    name_en: str | None
    name_ru: str | None
    name_variants: tuple[str, ...]
    kind: str | None
    parent_id: str | None
    staff_ids: frozenset[str]
    publication_ids: frozenset[str]

    @property
    def names(self) -> list[str]:
        seen: dict[str, None] = {}
        for name in (self.name_en, self.name_ru, *self.name_variants):
            if name and name.strip():
                seen.setdefault(name.strip(), None)
        return list(seen)

    def norms(self) -> list[NormName]:
        return [normalize(name) for name in self.names]


# ---------------------------------------------------------------------------
# lexical helpers (stdlib only — difflib, no fuzzy dependency)
# ---------------------------------------------------------------------------


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _token_sort_ratio(a: str, b: str) -> float:
    return _ratio(" ".join(sorted(a.split())), " ".join(sorted(b.split())))


def _token_set_ratio(a: str, b: str) -> float:
    """difflib approximation of rapidfuzz.token_set_ratio: compare the shared
    tokens against each side's full ordered token set."""
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    shared = " ".join(sorted(ta & tb))
    rest_a = " ".join(sorted(ta - tb))
    rest_b = " ".join(sorted(tb - ta))
    return max(
        _ratio(shared, f"{shared} {rest_a}".strip()),
        _ratio(shared, f"{shared} {rest_b}".strip()),
        _ratio(f"{shared} {rest_a}".strip(), f"{shared} {rest_b}".strip()),
    )


def _char_ngrams(text: str, n: int = 4) -> frozenset[str]:
    padded = f"  {text} "
    return frozenset(padded[i:i + n] for i in range(len(padded) - n + 1))


def _ngram_cosine(a: str, b: str) -> float:
    ga, gb = _char_ngrams(a), _char_ngrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / (len(ga) * len(gb)) ** 0.5


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if (a or b) else 0.0


# ---------------------------------------------------------------------------
# stage 1 — blocking
# ---------------------------------------------------------------------------

# A stem token attached to more names than this is a generic key ("optical",
# "system") — skip its bucket, real pairs are still caught by rarer tokens.
_GENERIC_TOKEN_CAP = 40
_NGRAM_BUCKET_CAP = 60
_NGRAM_OVERLAP_MIN = 0.30


def block(records: list[DepartmentRecord],
          embedder_pairs: set[tuple[str, str]] | None = None) -> set[tuple[str, str]]:
    """Candidate id pairs sharing a stem token, a char-4gram overlap, an
    acronym, or (if given) a semantic-neighbour pair from an embedder."""
    norms_by_id = {rec.id: rec.norms() for rec in records}
    by_token: dict[str, set[str]] = {}
    by_gram: dict[str, set[str]] = {}
    by_acronym: dict[str, set[str]] = {}
    grams_by_id: dict[str, frozenset[str]] = {}

    for rec in records:
        gram_union: set[str] = set()
        for norm in norms_by_id[rec.id]:
            for token in (norm.domain or norm.tokens):
                by_token.setdefault(token, set()).add(rec.id)
            grams = _char_ngrams(norm.text)
            gram_union |= grams
            for gram in grams:
                by_gram.setdefault(gram, set()).add(rec.id)
            for key in filter(None, (norm.acronym, norm.initials if 2 <= len(norm.initials) <= 7 else None)):
                by_acronym.setdefault(key, set()).add(rec.id)
        grams_by_id[rec.id] = frozenset(gram_union)

    pairs: set[tuple[str, str]] = set(embedder_pairs or set())

    for bucket in by_token.values():
        if len(bucket) <= _GENERIC_TOKEN_CAP:
            pairs.update(_ordered_pairs(bucket))
    for bucket in by_acronym.values():
        pairs.update(_ordered_pairs(bucket))
    for bucket in by_gram.values():
        if 2 <= len(bucket) <= _NGRAM_BUCKET_CAP:
            for a, b in _ordered_pairs(bucket):
                if _gram_overlap(grams_by_id[a], grams_by_id[b]) >= _NGRAM_OVERLAP_MIN:
                    pairs.add((a, b))
    return pairs


def _ordered_pairs(ids: set[str]):
    yield from combinations(sorted(ids), 2)


def _gram_overlap(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / min(len(a), len(b)) if (a and b) else 0.0


# ---------------------------------------------------------------------------
# stage 2 — signals + banding
# ---------------------------------------------------------------------------

# Hierarchy level of a `kind`. Two units on different levels are at best a
# parent/child link, never one unit under two names — so they never auto-merge
# and never share a merge group. "unit" is the catalog's "level unknown".
KIND_CLASS = {
    "megafaculty": "mega",
    "school": "top", "faculty": "top",
    "institute": "mid", "center": "mid",
    "department": "sub", "lab": "sub",
}


def kinds_compatible(a: str | None, b: str | None) -> bool:
    if not a or not b or "unit" in (a, b):
        return True
    return KIND_CLASS.get(a, a) == KIND_CLASS.get(b, b)


@dataclass(frozen=True)
class PairSignals:
    token_set: float
    token_sort: float
    levenshtein: float
    ngram_cosine: float
    jaccard_tokens: float
    jaccard_domain: float
    acronym_hit: bool
    guard_clear: bool          # domain tokens don't disagree -> safe to auto-merge
    head_diff: tuple[str, ...]  # the disagreeing domain tokens, for the journal
    kinds_compatible: bool
    shared_staff_ratio: float
    shared_publication_ratio: float
    embedding_cosine: float


def score_pair(a: DepartmentRecord, b: DepartmentRecord, embedding_cosine: float = 0.0) -> PairSignals:
    a_norms, b_norms = a.norms(), b.norms()
    best_text = max(
        ((na, nb) for na in a_norms for nb in b_norms),
        key=lambda pair: _token_set_ratio(pair[0].text, pair[1].text),
    )
    na, nb = best_text
    tokens_a = frozenset().union(*(n.tokens for n in a_norms))
    tokens_b = frozenset().union(*(n.tokens for n in b_norms))
    domain_a = frozenset().union(*(n.domain for n in a_norms))
    domain_b = frozenset().union(*(n.domain for n in b_norms))
    head_diff = tuple(sorted(domain_a ^ domain_b))
    jaccard_domain = _jaccard(domain_a, domain_b)
    guard_clear = not head_diff or (jaccard_domain >= 0.6 and len(head_diff) <= 1)

    acronym_hit = any(
        (x.acronym and x.acronym == y.initials) or (x.acronym and y.acronym and x.acronym == y.acronym)
        for x, y in ((na, nb), (nb, na))
    )
    shared_staff = len(a.staff_ids & b.staff_ids)
    shared_pubs = len(a.publication_ids & b.publication_ids)
    return PairSignals(
        token_set=_token_set_ratio(na.text, nb.text),
        token_sort=_token_sort_ratio(na.text, nb.text),
        levenshtein=_ratio(na.text, nb.text),
        ngram_cosine=_ngram_cosine(na.text, nb.text),
        jaccard_tokens=_jaccard(tokens_a, tokens_b),
        jaccard_domain=jaccard_domain,
        acronym_hit=acronym_hit,
        guard_clear=guard_clear,
        head_diff=head_diff,
        kinds_compatible=kinds_compatible(a.kind, b.kind),
        shared_staff_ratio=shared_staff / min(len(a.staff_ids), len(b.staff_ids)) if a.staff_ids and b.staff_ids else 0.0,
        shared_publication_ratio=shared_pubs / min(len(a.publication_ids), len(b.publication_ids))
        if a.publication_ids and b.publication_ids else 0.0,
        embedding_cosine=embedding_cosine,
    )


AUTO_MERGE = "auto-merge"
AUTO_REJECT = "auto-reject"
LLM = "llm"


def assign_band(sig: PairSignals) -> str:
    if not sig.kinds_compatible:
        # different level of the hierarchy — at best a parent/child link, which
        # only the LLM should propose; never an auto-merge.
        return LLM if max(sig.token_set, sig.embedding_cosine) >= 0.60 else AUTO_REJECT

    strong_lexical = sig.token_set >= 0.95 or sig.jaccard_domain >= 0.80
    strong_semantic = sig.embedding_cosine >= 0.90 and max(sig.token_set, sig.ngram_cosine) >= 0.40
    if sig.guard_clear and (strong_lexical or strong_semantic):
        return AUTO_MERGE
    if sig.acronym_hit and sig.guard_clear and max(sig.token_set, sig.embedding_cosine) >= 0.45:
        return AUTO_MERGE

    if sig.embedding_cosine >= 0.78:
        return LLM
    if not sig.guard_clear and sig.token_set < 0.80:
        return AUTO_REJECT
    if max(sig.token_set, sig.ngram_cosine, sig.levenshtein, sig.embedding_cosine) < 0.45:
        return AUTO_REJECT
    return LLM
