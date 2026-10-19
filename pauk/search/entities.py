"""Resolving a name from the question to a node: persons and departments.

OpenAlex spells people in Latin script, the question usually comes in
Cyrillic, and the Russian name fields are filled only for some authors -
so every name is compared both as written and transliterated.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz, process

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
}
# Spellings OpenAlex uses for the same sounds; folded so either matches.
_LATIN_FOLD = (("iy", "y"), ("ii", "i"), ("yi", "y"), ("ks", "x"), ("kh", "h"), ("j", "y"), ("w", "v"))
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)

AMBIGUITY_MARGIN = 3.0
MIN_SCORE = 80.0


def normalize(text: str) -> str:
    text = _NON_WORD.sub(" ", (text or "").casefold().replace("ё", "е"))
    return " ".join(text.split())


def transliterate(text: str) -> str:
    latin = "".join(_TRANSLIT.get(char, char) for char in normalize(text))
    for source, target in _LATIN_FOLD:
        latin = latin.replace(source, target)
    return latin


def name_key(text: str) -> str:
    """One comparable form for any spelling: lower-case Latin, folded."""
    return transliterate(text)


def initials_compatible(query: str, candidate: str) -> bool:
    """`ivanov i` fits `ivan ivanov` but not `petr ivanov`."""
    query_tokens, candidate_tokens = query.split(), candidate.split()
    initials = [token for token in query_tokens if len(token) == 1]
    words = [token for token in query_tokens if len(token) > 1]
    if not initials or not all(any(fuzz.ratio(word, other) >= 85 for other in candidate_tokens) for word in words):
        return not initials
    rest = [token for token in candidate_tokens if all(fuzz.ratio(token, word) < 85 for word in words)]
    return all(any(token.startswith(initial) for token in rest) for initial in initials)


@dataclass
class Candidate:
    id: str
    label: str
    score: float
    is_itmo: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Resolution:
    query: str
    kind: str
    candidates: list[Candidate]

    @property
    def best(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None

    @property
    def ambiguous(self) -> bool:
        best = self.best
        if best is None:
            return False
        rivals = [c for c in self.candidates[1:] if c.is_itmo == best.is_itmo]
        return any(best.score - c.score < AMBIGUITY_MARGIN for c in rivals)


class NameIndex:
    def __init__(self, kind: str, rows: list[dict[str, Any]], name_fields: tuple[str, ...]) -> None:
        self.kind = kind
        self.rows = {row["id"]: row for row in rows}
        self.keys: list[str] = []
        self.owners: list[str] = []
        for row in rows:
            for name in self._names(row, name_fields):
                key = name_key(name)
                if key:
                    self.keys.append(key)
                    self.owners.append(row["id"])

    @staticmethod
    def _names(row: dict[str, Any], name_fields: tuple[str, ...]) -> list[str]:
        names: list[str] = []
        for name_field in name_fields:
            value = row.get(name_field)
            if isinstance(value, list):
                names.extend(item for item in value if isinstance(item, str))
            elif isinstance(value, str):
                names.append(value)
        parts = [row.get("surname_ru"), row.get("first_name_ru"), row.get("second_name_ru")]
        if parts[0] and parts[1]:
            names.append(" ".join(part for part in parts if part))
        return list(dict.fromkeys(name for name in names if name.strip()))

    def resolve(self, query: str, *, limit: int = 8, prefer_itmo: bool = True) -> Resolution:
        key = name_key(query)
        if not key:
            return Resolution(query, self.kind, [])
        matches = process.extract(key, self.keys, scorer=fuzz.token_set_ratio, limit=limit * 20, score_cutoff=MIN_SCORE)
        best: dict[str, float] = {}
        for matched, score, index in matches:
            owner = self.owners[index]
            if self.kind == "person" and not initials_compatible(key, matched):
                continue
            # A full spelled-out match beats a subset match of the same score.
            score = score + 0.01 * fuzz.ratio(key, matched)
            best[owner] = max(best.get(owner, 0.0), score)
        candidates = [self._candidate(owner, score) for owner, score in best.items()]
        # Equally good names usually mean duplicate nodes of one person: the
        # one with more publications is the one the rest of the graph uses.
        candidates.sort(key=lambda c: (c.is_itmo if prefer_itmo else 0, round(c.score),
                                       c.extra.get("publications", 0), c.score), reverse=True)
        return Resolution(query, self.kind, candidates[:limit])

    def _candidate(self, owner: str, score: float) -> Candidate:
        row = self.rows[owner]
        variants = row.get("name_variants") or []
        label = row.get("name_ru") or row.get("name_en") or row.get("name_raw") or (variants[0] if variants else owner)
        extra = {key: row[key] for key in ("name_en", "publications", "kind") if row.get(key) is not None}
        return Candidate(owner, label, round(score, 2), bool(row.get("is_itmo")), extra)


def load_person_index(data_dir: Path) -> NameIndex:
    rows = json.loads((data_dir / "persons.json").read_text(encoding="utf-8"))
    return NameIndex("person", rows, ("name_en", "name_ru", "name_raw", "name_variants", "other_names"))


def load_department_index(data_dir: Path) -> NameIndex:
    rows = json.loads((data_dir / "departments.json").read_text(encoding="utf-8"))
    return NameIndex("department", rows, ("name_ru", "name_en", "name_variants", "context_aliases"))
