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
   a namesake must never inherit someone else's official record — and the
   people they blocked are written to russian_names_ambiguous.jsonl with
   their candidate records, since that is the one gap a human can close.

2. Transliteration fallback. Names the catalog does not know are
   reverse-transliterated ("Pavel Ivanov" -> "Павел Иванов") with a
   digraph-aware table built for English-romanized Russian names; the
   generic transliteration libraries were tried first and mangle exactly
   these ("Nikolay" -> "Николаы", "Julia" -> "Жулиа"). Only name_ru is
   set on this path — guessing name parts from word order is not
   reliable enough to store.

A catalog match is also an identity statement, not just a name: one row
is one employee, so two person records that resolve to the same row are
one researcher however their romanized names were spelled. The dedup
stage folds on that (see staff_id below and rule 4 in dedup.py), which
is why the matching lives here but is reachable before naming runs.

The catalog contains personal data and is therefore never committed; the
stage refuses to run (and thereby stops the pipeline) when the file is
missing. Default location: data/static/russian_names.csv, overridable
via PAUK_RUSSIAN_NAMES_FILE.
"""

from __future__ import annotations

import csv
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from pauk.models import Person
from pauk.models.processing import ProcessingState, ProcessingStatus
from pauk.storage.atomic import AtomicWriter

from .base import EnrichmentStage

logger = logging.getLogger(__name__)

CATALOG_FILENAME = "russian_names.csv"
AMBIGUOUS_FILENAME = "russian_names_ambiguous.jsonl"


def catalog_path(config) -> Path:
    """Where the staff catalog lives for this configuration."""
    return Path(config.russian_names_file or config.static_dir / CATALOG_FILENAME)


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
    # An author cited under the English form of their name carries a letter
    # the Russian one does not: Alexander is Александр, Valentine is
    # Валентин, Peter is Пётр.
    ("nder", "ndr"), ("ine", "in"), ("eter", "etr"),
)

# Cyrillic letters that look exactly like a Latin one. OpenAlex names arrive
# with a few of them mixed into an otherwise Latin spelling and the other way
# round, and the two alphabets do not fold alike — Cyrillic "В" gives "v"
# where Latin "B" gives "b" — so "V.A. Вogatyrev" never reaches its record.
_HOMOGLYPHS = {
    "А": "A", "В": "B", "С": "C", "Е": "E", "Н": "H", "К": "K", "М": "M",
    "О": "O", "Р": "P", "Т": "T", "Х": "X",
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y",
}
_HOMOGLYPHS_TO_CYRILLIC = {latin: cyrillic for cyrillic, latin in _HOMOGLYPHS.items()}


def _is_cyrillic(char: str) -> bool:
    return "Ѐ" <= char <= "ӿ"


def _alphabet_vote(text: str) -> int:
    """+1 when the text reads as Latin, -1 as Cyrillic, 0 when undecided.

    Homoglyphs cast no vote: they are the characters in question, and in
    "А. А. Маrmalyuk" they outnumber the letters that settle the spelling.
    """
    latin = sum(1 for char in text if char.isalpha() and not _is_cyrillic(char)
                and char not in _HOMOGLYPHS_TO_CYRILLIC)
    cyrillic = sum(1 for char in text if _is_cyrillic(char) and char not in _HOMOGLYPHS)
    return (latin > cyrillic) - (cyrillic > latin)


def _unmix_alphabets(value: str) -> str:
    """Move homoglyphs into the alphabet their own word is written in.

    The vote is per word rather than per name: "Maria Алексеевна Yaroslavova"
    is two Latin words around a Cyrillic one, and a whole-name vote rewrites
    the patronymic into a mixture that folds to a worse key than before. A
    word that is nothing but initials has no vote and follows the name.
    """
    whole = _alphabet_vote(value)
    words = []
    for word in value.split(" "):
        vote = _alphabet_vote(word) or whole
        table = _HOMOGLYPHS if vote > 0 else _HOMOGLYPHS_TO_CYRILLIC if vote < 0 else {}
        words.append("".join(table.get(char, char) for char in word))
    return " ".join(words)


def _fold(value: str) -> str:
    # Punctuation separates name parts rather than belonging to them: OpenAlex
    # serves "Ivanov, Ilya" beside "Ilya Ivanov", and a comma left in the
    # folded key keeps the two apart.
    folded = " ".join(_unmix_alphabets(value).replace(".", " ").replace(",", " ").split()).casefold()
    folded = "".join(_CYR_TO_LAT.get(ch, ch) for ch in folded)
    for src, dst in _FOLD_RULES:
        folded = folded.replace(src, dst)
    # A lone Latin "c" spells к in a romanized Russian name (Victoria,
    # Nicolay); in "ch" it is ч and in "sch" щ, so both keep their spelling.
    folded = re.sub(r"c(?!h)", "k", folded)
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
    if known is not None:
        return known if part[:1].isupper() else known.casefold()
    if "." in part:
        # "I.Yu." is two initials glued together, and each one is
        # capitalized on its own — converting the whole thing as one word
        # would leave "И.ю.".
        return ".".join(_word_to_cyrillic(piece) if piece else "" for piece in part.split("."))
    return _word_to_cyrillic(part)


def to_cyrillic(name: str) -> str:
    """Reverse-transliterate a romanized Russian name, word by word."""
    return " ".join(
        "-".join(_part_to_cyrillic(part) for part in word.split("-"))
        for word in name.split()
    )


# --- full names OpenAlex already carries in Cyrillic ----------------------------

# A patronymic suffix is not enough on its own: Бабич, Томкович and Ходасевич
# are surnames that end the same way. Position settles it — see _cyrillic_parts.
_PATRONYMIC = re.compile(
    r"^[А-ЯЁ][а-яё]+(?:ович|овна|евич|евна|ьевич|ьевна|иевич|иевна|инична|ична)$")
_CYRILLIC_WORD = re.compile(r"^[А-ЯЁ][а-яё]+$")


def _cyrillic_parts(name: str) -> tuple[str, str, str] | None:
    """(surname, given name, patronymic) when a name spells all three out.

    OpenAlex serves some authors under their Russian name, in either order
    ("Илья Алексеевич Суров", "Куликов Кирилл Сергеевич", and the same with
    a comma after the surname). Three full words are required: "М. В.
    Томкович" is initials and a surname that merely looks like a patronymic,
    and reading it as one would invent an patronymic for the person.
    """
    words = [word for word in name.replace(",", " ").split() if word]
    if len(words) != 3 or not all(_CYRILLIC_WORD.match(word) for word in words):
        return None
    first, second, third = words
    if _PATRONYMIC.match(second) and not _PATRONYMIC.match(third):
        return third, first, second
    if _PATRONYMIC.match(third) and not _PATRONYMIC.match(second):
        return first, second, third
    return None


def cyrillic_full_name(person: Person) -> tuple[str, str, str] | None:
    """The first spelling of this person that carries a full Russian name."""
    for name in (person.name_en, *person.name_variants):
        parts = _cyrillic_parts(name) if name else None
        if parts is not None:
            return parts
    return None


# --- staff catalog --------------------------------------------------------------


def _record_id(row: dict) -> str:
    """Stable identity of one catalog record, in the folded name space."""
    return "|".join(_fold(row.get(field) or "")
                    for field in ("surname", "name", "patronymic"))


class RussianNamesCatalog:
    """Official staff records indexed by folded romanized name keys."""

    def __init__(self, rows: list[dict]) -> None:
        rows = self._fold_repeated(rows)
        self.rows = rows
        keyed: dict[str, list[dict]] = {}
        spelled_out: set[str] = set()
        for row in rows:
            for key, spells_the_name_out in self._keyed_forms(row).items():
                keyed.setdefault(key, []).append(row)
                if spells_the_name_out:
                    spelled_out.add(key)
        # Two rows behind one key = namesakes: matching would hand one
        # person the other's official record. The key is unusable, but the
        # records behind it are what a human needs to resolve the case, so
        # they stay reachable for the review journal.
        self.by_key = {key: rows[0] for key, rows in keyed.items() if len(rows) == 1}
        self.ambiguous_by_key = {key: rows for key, rows in keyed.items() if len(rows) > 1}
        # Identity is claimed only from the forms that spell the given name
        # out. "A. Duhanov" is good enough to write a name onto a card, but
        # it stands for every Duhanov whose given name starts with an A —
        # including the ones this catalog does not list at all — so folding
        # two person records on it would be the namesake bug all over again.
        self.identity_by_key = {
            key: row for key, row in self.by_key.items() if key in spelled_out
        }

    @staticmethod
    def _fold_repeated(rows: list[dict]) -> list[dict]:
        """One row per employee, however many times the file lists them.

        A catalog assembled from several sources repeats people, and two
        rows naming one employee read as two namesakes: the key they share
        is dropped, and the person it describes stops matching altogether.
        Repeated rows are merged instead, the fuller value of each field
        winning, so a record with the degree filled in survives one without.
        """
        merged: dict[tuple[str, str, str], dict] = {}
        for row in rows:
            key = (_fold(row.get("surname") or ""), _fold(row.get("name") or ""),
                   _fold(row.get("patronymic") or ""))
            kept = merged.get(key)
            if kept is None:
                merged[key] = dict(row)
                continue
            for field, value in row.items():
                if (value or "").strip() and not (kept.get(field) or "").strip():
                    kept[field] = value
        return list(merged.values())

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

    @classmethod
    def load_if_present(cls, path: Path) -> RussianNamesCatalog | None:
        """The catalog when it is on disk, None when it is not.

        Naming cannot proceed without the catalog and calls load(); dedup
        treats it as one signal among several and has to keep working on
        deployments (and test runs) that do not carry the file.
        """
        return cls.load(path) if path.exists() else None

    @staticmethod
    def _initials(name: str, folded: str) -> set[str]:
        """Every folded form the initial of this name part can arrive as.

        A Cyrillic letter does not always fold to a single character, so an
        initial has two readings: "Ю" folds to "iu" and reaches a citation
        spelling it "Yu.", while a citation that shortened the same name to
        "Y." folds to "i" and needs the first character alone. Both are
        keyed — Юрьевна is 167 records here, and keying only one of them
        put all of them out of reach of half their citations.
        """
        return {form for form in (folded[:1], _fold(name[:1]) if name else "") if form}

    @classmethod
    def _keyed_forms(cls, row: dict) -> dict[str, bool]:
        """Every folded form of one record -> does it spell the given name out.

        The initials forms are listed last on purpose: when a record's own
        given name is a single letter, its "spelled out" form is the same
        string as its initials form, and the later, stricter value wins.
        """
        first_name = (row.get("name") or "").strip()
        patronymic_name = (row.get("patronymic") or "").strip()
        first = _fold(first_name)
        surname = _fold(row.get("surname") or "")
        patronymic = _fold(patronymic_name)
        if not first or not surname:
            return {}
        spelled = len(first) > 1
        first_initials = cls._initials(first_name, first)
        forms = {f"{first} {surname}": spelled, f"{surname} {first}": spelled}
        if patronymic:
            forms[f"{first} {patronymic} {surname}"] = spelled
            forms[f"{surname} {first} {patronymic}"] = spelled
            for patronymic_initial in cls._initials(patronymic_name, patronymic):
                forms[f"{first} {patronymic_initial} {surname}"] = spelled
                for first_initial in first_initials:
                    forms[f"{first_initial} {patronymic_initial} {surname}"] = False
        for first_initial in first_initials:
            forms[f"{first_initial} {surname}"] = False
        return forms

    def staff_id(self, person: Person) -> str | None:
        """The staff record this person certainly is, if the catalog says so.

        Unlike match(), which will name a card from an initials-only hit,
        this only answers on a form that spells the given name out — it is
        what dedup folds person records on — and only when no other spelling
        of the same person argues against the record.
        """
        for name in (person.name_en, *person.name_variants):
            if not name:
                continue
            row = self.identity_by_key.get(_fold(name))
            if row is not None:
                return None if self._contradicts(person.name_en, row) else _record_id(row)
        return None

    @staticmethod
    def _contradicts(name: str | None, row: dict) -> bool:
        """Whether this name states a patronymic the record does not have.

        "A. D. Dmitriev" is not Дмитриев Алексей Андреевич, however well
        another of his spellings ("Alexey Dmitriev") fits that record: in a
        name written down to initials the middle one is the only part left
        carrying information, and here it disagrees. Refusing costs a merge;
        accepting would hand one employee another's publications.

        Only the record's own display name is asked. OpenAlex collects
        display_name_alternatives from wherever an author was cited, so a
        single stray spelling from a mis-attributed paper sits in the list
        of half these people — enough to make one of them contradict
        anything, and not enough to be believed over the record itself.

        The match is a prefix test because an initial does not survive
        folding as one letter: "Yu." becomes "iu", "Zh." becomes "zh".
        """
        patronymic = _fold(row.get("patronymic") or "")
        surname = _fold(row.get("surname") or "")
        tokens = _fold(name or "").split()
        if not patronymic or len(tokens) != 3 or tokens[-1] != surname:
            return False
        return not patronymic.startswith(tokens[1])

    def match(self, person: Person) -> dict | None:
        """The official record to name this person from, if there is one.

        Looser than staff_id: an initials-only hit is enough to fill a card,
        because being wrong here shows a wrong patronymic rather than
        handing one employee another's publications. A record the display
        name argues against is refused all the same — writing "Дмитриев
        Алексей Андреевич" under "A. D. Dmitriev" states something about a
        real person that the one piece of evidence available denies.
        """
        for name in (person.name_en, *person.name_variants):
            if not name:
                continue
            row = self.by_key.get(_fold(name))
            if row is not None:
                return None if self._contradicts(person.name_en, row) else row
        return None

    def namesakes(self, person: Person) -> tuple[str, list[dict]] | None:
        """The catalog records this person's name could equally well be.

        Returned only when nothing matched: a name that fits several
        official records is the one case a human can actually resolve —
        the records differ by patronymic, and someone who knows the
        faculty can say which one it is.
        """
        names = [name for name in (person.name_en, *person.name_variants) if name]
        for name in names:
            rows = self.ambiguous_by_key.get(_fold(name))
            if rows and self._could_be_any_of(names, rows):
                return name, rows
        return None

    @staticmethod
    def _could_be_any_of(names: list[str], rows: list[dict]) -> bool:
        """Whether a written-out given name agrees with any candidate.

        Keys built from a first initial ("A. Polyakov") collide with every
        namesake in the catalog, so "Andrey Polyakov" trips over records
        for Anton and Alexander. The decision weighs every spelling the
        person is known by, not just the one that collided — an initials
        variant says nothing when the name is spelled out elsewhere and
        matches none of the records: that person is simply absent from the
        catalog, not a case anyone can resolve.
        """
        surnames = {_fold(row.get("surname") or "") for row in rows}
        given_names = {_fold(row.get("name") or "") for row in rows}
        spelled_out = {
            token for name in names for token in _fold(name).split()
            if len(token) > 1 and token not in surnames
        }
        return not spelled_out or bool(spelled_out & given_names)


class RussianNamesStage(EnrichmentStage):
    name = "russian_names"

    def run(self) -> dict[str, int]:
        catalog = RussianNamesCatalog.load(catalog_path(self.config))

        people = list(self.prepared.read_models("persons", Person))
        changed = matched = transliterated = from_own_spelling = 0
        ambiguous: list[dict] = []
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
            elif (parts := cyrillic_full_name(person)) is not None:
                # The catalog does not know this person, but one of their
                # own spellings does: a name written out in Cyrillic states
                # the patronymic the transliteration path can never guess.
                surname, first_name, patronymic = parts
                person.surname_ru = surname
                person.first_name_ru = first_name
                person.second_name_ru = patronymic
                person.name_ru = f"{surname} {first_name} {patronymic}"
                from_own_spelling += 1
            else:
                person.name_ru = to_cyrillic(person.name_en)
                if person.surname_ru:
                    # Only a catalog match sets the parts, so finding them
                    # here means an earlier run reached a record this one
                    # refuses. They are that decision's output and go with
                    # it: author_label reads them ahead of name_ru and
                    # would keep signing the card from a withdrawn record.
                    # A merge that brought a matching spelling in gets them
                    # back through that spelling on this very pass.
                    person.first_name_ru = None
                    person.second_name_ru = None
                    person.surname_ru = None
                    person.degree = None
                transliterated += 1
                collision = catalog.namesakes(person)
                if collision is not None:
                    matched_name, rows = collision
                    ambiguous.append({
                        "person": person.id,
                        "name_en": person.name_en,
                        "matched_name": matched_name,
                        "name_ru": person.name_ru,
                        "candidates": [
                            {"name_ru": (row.get("name_ru") or "").strip(),
                             "degree": (row.get("degree") or "").strip() or None}
                            for row in rows
                        ],
                        "held_because": "the catalog holds several records under this name",
                    })
            person.processing[self.name] = self._state(state, ProcessingStatus.COMPLETED, 1)
            changed += 1
        if changed:
            self.prepared.write_models("persons", people)

        # The journal describes the people this run looked at, so a run that
        # looked at nobody leaves it alone: a second pass over a finished
        # group would otherwise empty the one artefact a human works from.
        # A partial run rewrites only its own rows and keeps the rest.
        journal_path = self.prepared.group_dir / AMBIGUOUS_FILENAME
        if changed:
            examined = {person.id for person in people
                        if person.processing.get(self.name) is not None}
            journal = [row for row in self._journalled(journal_path)
                       if row.get("person") not in examined]
            journal.extend(ambiguous)
            with AtomicWriter(journal_path) as fh:
                for row in journal:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        logger.info("russian_names: %d from the catalog, %d from the author's own spelling, "
                    "%d transliterated", matched, from_own_spelling, transliterated)
        if ambiguous:
            logger.info("russian_names: %d name(s) fit several catalog records — see %s",
                        len(ambiguous), journal_path)
        return {"russian_names": changed, "names_from_catalog": matched,
                "names_from_own_spelling": from_own_spelling,
                "names_transliterated": transliterated,
                "names_ambiguous": len(ambiguous)}

    @staticmethod
    def _journalled(path: Path) -> list[dict]:
        """Rows a previous run left in the ambiguity journal."""
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    @staticmethod
    def _state(previous: ProcessingState | None, status: ProcessingStatus,
               result_count: int) -> ProcessingState:
        return ProcessingState(
            status=status,
            attempts=(previous.attempts if previous else 0) + 1,
            finished_at=datetime.now(timezone.utc),
            result_count=result_count,
        )
