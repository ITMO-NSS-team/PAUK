"""Russian full names for authors.

The GUI shows ITMO people to a Russian-speaking audience, but OpenAlex
serves romanized names ("Nikolay O. Nikitin"). This stage restores the
Cyrillic form in two steps:

1. Catalog match. A CSV of official ITMO staff records (columns:
   name_ru, surname, name, patronymic, degree) is matched against the
   person's display name and variants. Matching happens in a shared
   folded-transliteration space that survives romanization differences
   (Alexey/Aleksei, Yulia/Julia/Iuliia) and initials ("N. O. Nikitin").
   A match fills the official full name, its parts and the academic
   degree. Keys that fit more than one catalog row are dropped entirely —
   a namesake must never inherit someone else's official record.

2. Transliteration fallback. Names the catalog does not know are
   reverse-transliterated ("Pavel Ivanov" -> "Павел Иванов") with a
   digraph-aware table built for English-romanized Russian names; the
   generic transliteration libraries were tried first and mangle exactly
   these ("Nikolay" -> "Николаы", "Julia" -> "Жулиа"). Only name_ru is
   set on this path — guessing name parts from word order is not
   reliable enough to store.

The catalog contains personal data and is therefore never committed; the
stage refuses to run (and thereby stops the pipeline) when the file is
missing. Default location: data/static/russian_names.csv, overridable
via PAUK_RUSSIAN_NAMES_FILE.
"""

from __future__ import annotations

import csv
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from pauk.models import Person
from pauk.models.processing import ProcessingState, ProcessingStatus

from .base import EnrichmentStage

logger = logging.getLogger(__name__)

CATALOG_FILENAME = "russian_names.csv"

# --- folded transliteration space for matching --------------------------------

_CYR_TO_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "i", "ь": "", "э": "e", "ю": "iu", "я": "ia",
}

# Romanization variants collapse onto one key: Alexey/Aleksei, Yuri/Yury,
# Mikhail/Mihail, Iuliia/Yulia/Julia all fold to the same string.
_FOLD_RULES = (
    ("shch", "sch"), ("kh", "h"), ("x", "ks"),
    ("yo", "e"), ("yu", "iu"), ("ya", "ia"),
    ("j", "i"), ("y", "i"),
)


def _fold(value: str) -> str:
    folded = " ".join(value.replace(".", " ").split()).casefold()
    folded = "".join(_CYR_TO_LAT.get(ch, ch) for ch in folded)
    for src, dst in _FOLD_RULES:
        folded = folded.replace(src, dst)
    return re.sub(r"i{2,}", "i", folded)


# --- reverse transliteration (romanized Russian -> Cyrillic) -------------------

_REVERSE_RULES: tuple[tuple[str, str], ...] = (
    ("shch", "щ"), ("sch", "щ"),
    ("yo", "ё"), ("jo", "ё"),
    # A y/i closing a diphthong before a consonant is й (Zaytsev, Voytenko,
    # Seyfullin). Before a vowel it is not (Nikolayev stays Николаев), and
    # plain "ai" only closes one before "ts" — otherwise Mikhail would turn
    # into Михайл.
    (r"ay(?=[bcdfghjklmnpqrstvwxz])", "ай"),
    (r"ey(?=[bcdfghjklmnpqrstvwxz])", "ей"),
    (r"oy(?=[bcdfghjklmnpqrstvwxz])", "ой"),
    (r"ai(?=ts)", "ай"), (r"ei(?=ts)", "ей"),
    ("zh", "ж"), ("kh", "х"), ("ts", "ц"), ("ch", "ч"), ("sh", "ш"),
    # ya spells ья only after the consonants where a soft sign dominates
    # (Ulyanov, Lukyanov, Kasyanov, Tretyakov, Dyakonov). After the others
    # it is plain я — Kudryavtsev, Ryabov, Myasnikov — and yu after any
    # consonant is plain ю (Kolyubin, Klyuev).
    (r"(?<=[dklnstz])ya", "ья"),
    ("yu", "ю"), ("ju", "ю"), ("ya", "я"), ("ja", "я"),
    (r"^iu", "ю"),
    (r"iy$", "ий"), (r"ij$", "ий"), (r"yy$", "ый"), (r"yj$", "ый"),
    # A closing "yi" is the ый ending (Rudyi, Bezrodnyi); inside a word the
    # same pair is a soft sign (Ilyina).
    (r"yi$", "ый"), (r"(?<=[bcdfghklmnpqrstvwxz])yi", "ьи"),
    (r"iia$", "ия"), (r"ii$", "ий"),
    (r"aia$", "ая"), (r"ia$", "ия"),
    (r"ei$", "ей"), (r"ai$", "ай"),
    (r"(?<=[bcdfghklmnpqrstvwxz])y$", "ий"),
    # Two-char lookbehind keeps two-letter names ("Li") out of this rule.
    (r"(?<=\w[bcdfghklmnpqrstvwxz])i$", "ий"),
    # "y" joining two vowels is a hiatus filler, not a letter of its own
    # (Nikolayev, Sergeyev); after a consonant before "e" it is a soft sign
    # (Vasilyev, Grigoryev).
    (r"(?<=[bcdfghklmnpqrstvwxz])y(?=e)", "ь"),
    (r"(?<=[aeiou])y(?=[aeiou])", ""),
    # Leading "ye" is е (Yevgeny, Yelena); leading ya/yu are already gone.
    (r"^ye", "е"), (r"y$", "й"),
    ("x", "кс"),
)

# Common given names whose per-character transliteration comes out wrong
# ("Ilya" -> "Илия", "Olga" -> "Олга", "Alexander" -> "Александер"). Keyed
# by the folded romanized form, so every spelling variant (Ilya/Ilia/Ilja,
# Pyotr/Petr, Tatiana/Tatyana) lands on one entry. These are common first
# names, not personal data — they can live in the repository.
_GIVEN_NAMES = {
    "ilia": "Илья",
    "olga": "Ольга",
    "igor": "Игорь",
    "daria": "Дарья",
    "tatiana": "Татьяна",
    "petr": "Пётр",
    "piotr": "Пётр",
    "fedor": "Фёдор",
    "semen": "Семён",
    "liubov": "Любовь",
    "ielena": "Елена",
    "iakov": "Яков",
    "aleksander": "Александр",
    "aleksandr": "Александр",
    "eugene": "Евгений",
    "viacheslav": "Вячеслав",
    "liudmila": "Людмила",
    "eduard": "Эдуард",
    "artem": "Артём",
    "olesia": "Олеся",
    "nikita": "Никита",
    "lev": "Лев",
}

_SINGLE = {
    "a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф", "g": "г",
    "h": "х", "i": "и", "j": "й", "k": "к", "l": "л", "m": "м", "n": "н",
    "o": "о", "p": "п", "q": "к", "r": "р", "s": "с", "t": "т", "u": "у",
    "v": "в", "w": "в", "y": "ы", "z": "з", "'": "ь", "’": "ь",
}


def _word_to_cyrillic(word: str) -> str:
    lower = word.casefold()
    for pattern, replacement in _REVERSE_RULES:
        # "Yuri" -> "юri" -> "юrий": a rule may fire on a partially
        # converted string, so lookbehinds list Latin consonants only.
        lower = re.sub(pattern, replacement, lower)
    converted = "".join(_SINGLE.get(ch, ch) for ch in lower)
    # "S.S." is initials, not a word that merely starts with a capital.
    if word.isupper():
        return converted.upper()
    if word[:1].isupper():
        converted = converted[:1].upper() + converted[1:]
    return converted


def _part_to_cyrillic(part: str) -> str:
    known = _GIVEN_NAMES.get(_fold(part))
    if known is None:
        return _word_to_cyrillic(part)
    return known if part[:1].isupper() else known.casefold()


def to_cyrillic(name: str) -> str:
    """Reverse-transliterate a romanized Russian name, word by word."""
    return " ".join(
        "-".join(_part_to_cyrillic(part) for part in word.split("-"))
        for word in name.split()
    )


# --- staff catalog --------------------------------------------------------------


class RussianNamesCatalog:
    """Official staff records indexed by folded romanized name keys."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        keyed: dict[str, dict | None] = {}
        for row in rows:
            for key in self._keys(row):
                # Two rows behind one key = namesakes: matching would hand
                # one person the other's official record. Drop the key.
                keyed[key] = row if key not in keyed else None
        self.by_key = {key: row for key, row in keyed.items() if row is not None}

    @classmethod
    def load(cls, path: Path) -> "RussianNamesCatalog":
        if not path.exists():
            raise FileNotFoundError(
                f"russian names catalog not found: {path} — the file is kept out of "
                "the repository (personal data); place it there or point "
                "PAUK_RUSSIAN_NAMES_FILE at it")
        with path.open(encoding="utf-8-sig", newline="") as fh:
            rows = [row for row in csv.DictReader(fh) if (row.get("surname") or "").strip()]
        return cls(rows)

    @staticmethod
    def _keys(row: dict) -> set[str]:
        first = _fold(row.get("name") or "")
        surname = _fold(row.get("surname") or "")
        patronymic = _fold(row.get("patronymic") or "")
        if not first or not surname:
            return set()
        keys = {f"{first} {surname}", f"{surname} {first}"}
        if patronymic:
            keys |= {
                f"{first} {patronymic} {surname}",
                f"{surname} {first} {patronymic}",
                f"{first} {patronymic[:1]} {surname}",
                f"{first[:1]} {patronymic[:1]} {surname}",
            }
        keys.add(f"{first[:1]} {surname}")
        return keys

    def match(self, person: Person) -> dict | None:
        for name in (person.name_en, *person.name_variants):
            if not name:
                continue
            row = self.by_key.get(_fold(name))
            if row is not None:
                return row
        return None


class RussianNamesStage(EnrichmentStage):
    name = "russian_names"

    def run(self) -> dict[str, int]:
        catalog_path = Path(self.config.russian_names_file or
                            self.config.static_dir / CATALOG_FILENAME)
        catalog = RussianNamesCatalog.load(catalog_path)

        people = list(self.prepared.read_models("persons", Person))
        changed = matched = transliterated = 0
        for person in people:
            if not self.selected("persons", person.id):
                continue
            state = person.processing.get(self.name)
            if not self.needs_attempt(state):
                continue
            if not person.name_en:
                person.processing[self.name] = self._state(state, ProcessingStatus.COMPLETED_EMPTY, 0)
                changed += 1
                continue
            row = catalog.match(person)
            if row is not None:
                person.name_ru = (row.get("name_ru") or "").strip() or person.name_ru
                person.first_name_ru = (row.get("name") or "").strip() or None
                person.second_name_ru = (row.get("patronymic") or "").strip() or None
                person.surname_ru = (row.get("surname") or "").strip() or None
                person.degree = person.degree or (row.get("degree") or "").strip() or None
                matched += 1
            else:
                person.name_ru = to_cyrillic(person.name_en)
                transliterated += 1
            person.processing[self.name] = self._state(state, ProcessingStatus.COMPLETED, 1)
            changed += 1
        if changed:
            self.prepared.write_models("persons", people)
        logger.info("russian_names: %d from the catalog, %d transliterated",
                    matched, transliterated)
        return {"russian_names": changed, "names_from_catalog": matched,
                "names_transliterated": transliterated}

    @staticmethod
    def _state(previous: ProcessingState | None, status: ProcessingStatus,
               result_count: int) -> ProcessingState:
        return ProcessingState(
            status=status,
            attempts=(previous.attempts if previous else 0) + 1,
            finished_at=datetime.now(timezone.utc),
            result_count=result_count,
        )
