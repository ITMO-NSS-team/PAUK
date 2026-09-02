"""Stage 0 — normalize one department name string.

A name reaches the graph from an affiliation string, so the same unit is
written many ways: British/US spelling, word order, morphology, typos,
Russian vs English, acronyms. `normalize` folds the mechanical variation and
splits off the *domain* tokens (what the unit studies) from the *qualifier*
tokens (that it is a "center" / "laboratory" / "international" ...), because
the later over-merge guard must look only at the domain part: "AI in
Chemistry" and "AI in Agrobiotechnology" differ by one domain token and are
different units, "Educational Neuroscience" and "Neuroscience in Education"
differ by none and are one.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Cyrillic letters that render identically to a Latin one. OpenAlex serves the
# same name both ways; see pauk.pipeline.stages.author_names._unmix_alphabets.
_HOMOGLYPHS = str.maketrans({
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O",
    "Р": "P", "С": "C", "Т": "T", "Х": "X", "а": "a", "е": "e", "о": "o",
    "р": "p", "с": "c", "х": "x", "у": "y",
})

# Function words that carry no unit identity.
_STOPWORDS = frozenset({
    "of", "for", "the", "and", "in", "on", "to", "a", "an", "at", "de",
    "и", "в", "по", "на", "с", "для", "к",
})

# Words that name the *kind* of unit, not *which* unit. The over-merge guard
# ignores these and compares only the domain tokens that remain.
_QUALIFIER_TOKENS = frozenset({
    "center", "centre", "faculty", "institute", "laboratory", "lab", "school",
    "department", "unit", "megafaculty", "international", "research", "scientific",
    "educational", "education", "national", "higher", "joint", "interfaculty",
    "cross", "industry", "transnational", "production", "engineering",
    "technolog", "technology", "system", "science", "sciences",
    "центр", "факультет", "институт", "лаборатория", "школа", "кафедра",
    "международный", "международная", "научный", "научная", "научно",
    "исследовательский", "образовательный", "национальный", "мегафакультет",
    "высшая", "передовая",
})

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_INITIALS_ACRONYM = re.compile(r"^\(?([A-Za-zА-Яа-я]{2,7})\)?$")
_TRAILING_ACRONYM = re.compile(r"\(([A-Za-zА-Яа-я]{2,7})\)\s*$")


def _transliterate(token: str) -> str:
    return "".join(_TRANSLIT.get(ch, ch) for ch in token)


def _stem(token: str) -> str:
    """Crude, language-agnostic suffix folding: enough to land Technologies /
    Technology / технологий on one stem for token comparison."""
    stem = token
    for british, american in (("centre", "center"), ("programme", "program"),
                              ("modelling", "modeling"), ("optimisation", "optimization")):
        stem = stem.replace(british, american)
    if len(stem) > 4 and stem.endswith("ies"):
        stem = stem[:-3] + "y"
    if len(stem) > 4 and stem.endswith(("es", "s")):
        stem = stem[:-2] if stem.endswith("es") else stem[:-1]
    stem = _transliterate(stem)
    if stem.startswith(("tekhnolog", "technolog")):
        return "technolog"
    if stem.startswith(("nanotekhnolog", "nanotechnolog")):
        return "nanotechnolog"
    return stem[:8]


def is_cyrillic(text: str) -> bool:
    letters = [ch for ch in text if ch.isalpha()]
    cyrillic = sum("CYRILLIC" in unicodedata.name(ch, "") for ch in letters)
    return bool(letters) and cyrillic > len(letters) / 2


def _acronym(raw: str) -> str | None:
    """An explicit acronym written as the whole name ("FBIT", "СЦ") or a
    trailing "(SCAMT)" — not initials we synthesised from the words."""
    stripped = raw.strip().replace(".", "")
    match = _INITIALS_ACRONYM.match(stripped)
    if match and match.group(1).isupper():
        return match.group(1).upper()
    trailing = _TRAILING_ACRONYM.search(raw.strip())
    return trailing.group(1).upper() if trailing else None


@dataclass(frozen=True)
class NormName:
    """One normalized name string."""

    raw: str
    text: str                # stemmed significant tokens joined by spaces
    tokens: frozenset[str]   # stemmed significant tokens
    domain: frozenset[str]   # tokens minus qualifiers — what the guard compares
    acronym: str | None      # explicit acronym in the string, if any
    initials: str            # first letters of the significant tokens
    cyrillic: bool


def normalize(name: str) -> NormName:
    folded = name.translate(_HOMOGLYPHS).casefold().replace("&", " and ").replace("-", " ")
    raw_tokens = _WORD.findall(folded)
    significant = [tok for tok in raw_tokens if tok not in _STOPWORDS]
    stems = [_stem(tok) for tok in significant]
    tokens = frozenset(stems)
    return NormName(
        raw=name,
        text=" ".join(stems),
        tokens=tokens,
        domain=frozenset(tok for tok in tokens if tok not in _QUALIFIER_TOKENS),
        acronym=_acronym(name),
        initials="".join(tok[0] for tok in significant).upper(),
        cyrillic=is_cyrillic(name),
    )
