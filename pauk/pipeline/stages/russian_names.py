"""Russian and English name parts for authors.

The GUI shows ITMO people to a Russian-speaking audience, but OpenAlex
serves a single romanized display name ("Nikolay O. Nikitin", sometimes
Cyrillic, sometimes mixed) — name_raw. This stage splits it, and every
known spelling variant, into surname/first name/second name (patronymic),
in both Russian and English:

1. Catalog match, for identity and the academic degree only. A CSV of
   official ITMO staff records (columns: name_ru, surname, name,
   patronymic, degree) is matched against the person's display name and
   variants in a shared folded-transliteration space that survives
   romanization differences (Alexey/Aleksei, Yulia/Julia/Iuliia) and
   initials ("N. O. Nikitin"). A match fills the academic degree.
   Keys that fit more than one catalog row are dropped entirely — a
   namesake must never inherit someone else's official record.

2. LLM name split. Every person needing a fresh attempt is sent to an LLM
   (self.config.llm_model) with their name_raw, name_variants, and every
   catalog row sharing a folded surname token — a broader net than the
   strict catalog match above, so the model can disambiguate namesakes the
   way a human reviewer would (rule 1 of NAME_SPLIT_PROMPT: prefer a
   plausible candidate, verbatim). The reply is validated by three
   deterministic guards before it is trusted: _guard_invented_second_name
   drops a patronymic invented from a bare initial (models in this class
   do this readily despite the prompt's explicit ban — the same risk this
   module refused to take when the logic was hand-written);
   _guard_misclassified_second_name folds a second_name back into
   first_name when it doesn't morphologically look like a patronymic and
   nothing confirms it (naming traditions with more than one given name and
   no patronymic at all — Spanish "Pedro Luis González" — otherwise get a
   given name mislabeled as one); and _guard_broken_transliteration drops
   or fixes a *_ru field that isn't actually in Cyrillic. A failed LLM call
   falls back to reverse transliteration ("Pavel Ivanov" -> "Павел Иванов",
   to_cyrillic) for name_ru only, and is retried on the next pipeline run.

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
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from pauk.models import Person
from pauk.models.processing import ProcessingState, ProcessingStatus
from pauk.sources import OpenRouterClient
from pauk.storage import LlmLogStore

from .base import EnrichmentStage

logger = logging.getLogger(__name__)

CATALOG_FILENAME = "russian_names.csv"


def catalog_path(config) -> Path:
    """Where the staff catalog lives for this configuration."""
    return Path(config.russian_names_file or config.static_dir / CATALOG_FILENAME)


# --- folded transliteration space for matching --------------------------------

_CYR_TO_LAT = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "i",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "h",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "sch",
    "ъ": "",
    "ы": "i",
    "ь": "",
    "э": "e",
    "ю": "iu",
    "я": "ia",
}
_FOLD_RULES = (
    ("shch", "sch"),
    ("kh", "h"),
    ("x", "ks"),
    ("yo", "e"),
    ("yu", "iu"),
    ("ya", "ia"),
    ("j", "i"),
    ("y", "i"),
    ("nder", "ndr"),
    ("ine", "in"),
    ("eter", "etr"),
)

_HOMOGLYPHS = {
    "А": "A",
    "В": "B",
    "С": "C",
    "Е": "E",
    "Н": "H",
    "К": "K",
    "М": "M",
    "О": "O",
    "Р": "P",
    "Т": "T",
    "Х": "X",
    "а": "a",
    "е": "e",
    "о": "o",
    "р": "p",
    "с": "c",
    "х": "x",
    "у": "y",
}
_HOMOGLYPHS_TO_CYRILLIC = {latin: cyrillic for cyrillic, latin in _HOMOGLYPHS.items()}


def _is_cyrillic(char: str) -> bool:
    return "Ѐ" <= char <= "ӿ"


def _alphabet_vote(text: str) -> int:
    """+1 when the text reads as Latin, -1 as Cyrillic, 0 when undecided.

    Homoglyphs cast no vote: they are the characters in question, and in
    "А. А. Маrmalyuk" they outnumber the letters that settle the spelling.
    """
    latin = sum(
        1
        for char in text
        if char.isalpha() and not _is_cyrillic(char) and char not in _HOMOGLYPHS_TO_CYRILLIC
    )
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
    folded = " ".join(
        _unmix_alphabets(value).replace(".", " ").replace(",", " ").split()
    ).casefold()
    folded = "".join(_CYR_TO_LAT.get(ch, ch) for ch in folded)
    for src, dst in _FOLD_RULES:
        folded = folded.replace(src, dst)
    # A lone Latin "c" spells к in a romanized Russian name (Victoria,
    # Nicolay); in "ch" it is ч and in "sch" щ, so both keep their spelling.
    folded = re.sub(r"c(?!h)", "k", folded)
    return re.sub(r"i{2,}", "i", folded)


# --- reverse transliteration (romanized Russian -> Cyrillic) -------------------

_REVERSE_RULES: tuple[tuple[str, str], ...] = (
    ("shch", "щ"),
    ("sch", "щ"),
    ("yo", "ё"),
    ("jo", "ё"),
    # A y/i closing a diphthong before a consonant is й (Zaytsev, Voytenko,
    # Seyfullin). Before a vowel it is not (Nikolayev stays Николаев), and
    # plain "ai" only closes one before "ts" — otherwise Mikhail would turn
    # into Михайл.
    (r"ay(?=[bcdfghjklmnpqrstvwxz])", "ай"),
    (r"ey(?=[bcdfghjklmnpqrstvwxz])", "ей"),
    (r"oy(?=[bcdfghjklmnpqrstvwxz])", "ой"),
    (r"ai(?=ts)", "ай"),
    (r"ei(?=ts)", "ей"),
    ("zh", "ж"),
    ("kh", "х"),
    ("ts", "ц"),
    ("ch", "ч"),
    ("sh", "ш"),
    # ya spells ья only after the consonants where a soft sign dominates
    # (Ulyanov, Lukyanov, Kasyanov, Tretyakov, Dyakonov). After the others
    # it is plain я — Kudryavtsev, Ryabov, Myasnikov — and yu after any
    # consonant is plain ю (Kolyubin, Klyuev).
    (r"(?<=[dklnstz])ya", "ья"),
    ("yu", "ю"),
    ("ju", "ю"),
    ("ya", "я"),
    ("ja", "я"),
    (r"^iu", "ю"),
    (r"iy$", "ий"),
    (r"ij$", "ий"),
    (r"yy$", "ый"),
    (r"yj$", "ый"),
    # A closing "yi" is the ый ending (Rudyi, Bezrodnyi); inside a word the
    # same pair is a soft sign (Ilyina).
    (r"yi$", "ый"),
    (r"(?<=[bcdfghklmnpqrstvwxz])yi", "ьи"),
    (r"iia$", "ия"),
    (r"ii$", "ий"),
    (r"aia$", "ая"),
    (r"ia$", "ия"),
    (r"ei$", "ей"),
    (r"ai$", "ай"),
    (r"(?<=[bcdfghklmnpqrstvwxz])y$", "ий"),
    # Two-char lookbehind keeps two-letter names ("Li") out of this rule.
    (r"(?<=\w[bcdfghklmnpqrstvwxz])i$", "ий"),
    # "y" joining two vowels is a hiatus filler, not a letter of its own
    # (Nikolayev, Sergeyev); after a consonant before "e" it is a soft sign
    # (Vasilyev, Grigoryev).
    (r"(?<=[bcdfghklmnpqrstvwxz])y(?=e)", "ь"),
    (r"(?<=[aeiou])y(?=[aeiou])", ""),
    # Leading "ye" is е (Yevgeny, Yelena); leading ya/yu are already gone.
    (r"^ye", "е"),
    (r"y$", "й"),
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
    "a": "а",
    "b": "б",
    "c": "к",
    "d": "д",
    "e": "е",
    "f": "ф",
    "g": "г",
    "h": "х",
    "i": "и",
    "j": "й",
    "k": "к",
    "l": "л",
    "m": "м",
    "n": "н",
    "o": "о",
    "p": "п",
    "q": "к",
    "r": "р",
    "s": "с",
    "t": "т",
    "u": "у",
    "v": "в",
    "w": "в",
    "y": "ы",
    "z": "з",
    "'": "ь",
    "’": "ь",
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
        "-".join(_part_to_cyrillic(part) for part in word.split("-")) for word in name.split()
    )


# --- staff catalog --------------------------------------------------------------


def _record_id(row: dict) -> str:
    """Stable identity of one catalog record, in the folded name space."""
    return "|".join(_fold(row.get(field) or "") for field in ("surname", "name", "patronymic"))


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
        # person the other's official record, so the key is unusable for
        # match()/staff_id() below - kept out of by_key entirely.
        self.by_key = {key: rows[0] for key, rows in keyed.items() if len(rows) == 1}
        # Identity is claimed only from the forms that spell the given name
        # out. "A. Duhanov" is good enough to write a name onto a card, but
        # it stands for every Duhanov whose given name starts with an A —
        # including the ones this catalog does not list at all — so folding
        # two person records on it would be the namesake bug all over again.
        self.identity_by_key = {key: row for key, row in self.by_key.items() if key in spelled_out}

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
            key = (
                _fold(row.get("surname") or ""),
                _fold(row.get("name") or ""),
                _fold(row.get("patronymic") or ""),
            )
            kept = merged.get(key)
            if kept is None:
                merged[key] = dict(row)
                continue
            for field, value in row.items():
                if (value or "").strip() and not (kept.get(field) or "").strip():
                    kept[field] = value
        return list(merged.values())

    @classmethod
    def load(cls, path: Path) -> RussianNamesCatalog:
        if not path.exists():
            raise FileNotFoundError(
                f"russian names catalog not found: {path} — the file is kept out of "
                "the repository (personal data); place it there or point "
                "PAUK_RUSSIAN_NAMES_FILE at it"
            )
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
                    forms[f"{surname} {first_initial} {patronymic_initial}"] = False
        for first_initial in first_initials:
            forms[f"{first_initial} {surname}"] = False
            forms[f"{surname} {first_initial}"] = False
        return forms

    def staff_id(self, person: Person) -> str | None:
        """The staff record this person certainly is, if the catalog says so.

        Unlike match(), which will name a card from an initials-only hit,
        this only answers on a form that spells the given name out — it is
        what dedup folds person records on — and only when no other spelling
        of the same person argues against the record.
        """
        for name in (person.name_raw, *person.name_variants):
            if not name:
                continue
            row = self.identity_by_key.get(_fold(name))
            if row is not None:
                return None if self._contradicts(person.name_raw, row) else _record_id(row)
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
        for name in (person.name_raw, *person.name_variants):
            if not name:
                continue
            row = self.by_key.get(_fold(name))
            if row is not None:
                return None if self._contradicts(person.name_raw, row) else row
        return None


# --- LLM name split ---------------------------------------------------------------

NAME_SPLIT_PROMPT = """You are extracting an author's name for an ITMO research publication,
split into surname / first name / second name (patronymic), separately in Russian and
in English.

Raw name as given by the data source (OpenAlex) - may be Latin script, Cyrillic, or mixed:
  {name_raw}

Other known spellings of the same person (sometimes one of these spells the name out more fully):
  {variants}

Candidate records from the official ITMO staff directory sharing the same surname
(a candidate may be a namesake - not necessarily the same person):
{candidates}

Task: extract the person's surname, first name and second name (patronymic), separately in
Russian and in English.

STRICT rules - follow exactly, do not deviate:
1. Check the candidate list FIRST, before anything else. If a candidate is plausibly this
   same person (surname matches and nothing about the first name/second name contradicts
   it), set matched_candidate to its index and copy its surname/name/patronymic verbatim
   into the *_ru fields - the directory is ground truth. Do not retype, correct, or
   rephrase them.
2. If several candidates could plausibly be this person and you cannot tell which, set
   matched_candidate to null. Never guess between namesakes.
3. Extraction only, never invention of MISSING information:
   - If a name part is spelled out in full somewhere in the input, extract it in full.
   - If a name part appears ONLY as a bare initial (e.g. "I." in "I. Ivanov"), extract
     exactly that: a single letter, WITHOUT a period. Do not expand an initial into a full
     name you are not certain of - not even a "standard" or "common" expansion, and not from
     anything you may know about a real person of that name. A bare initial with no matching
     candidate stays a bare initial.
   - If a name part does not appear anywhere in the input and no matched candidate resolves
     it, leave it null. Do not guess a plausible-sounding value.
4. surname_ru and first_name_ru (and second_name_ru when a patronymic is known) must ALWAYS
   be filled, in Cyrillic, for every person - Russian, foreign, anyone. This is a practical
   Russian transcription of whatever name you extracted in rule 3, not a judgement about the
   person's nationality: "Salvy Russo" still gets a surname_ru/first_name_ru (a natural
   Russian transcription, e.g. "Руссо"/"Сальви"). Use the conventional Russian transcription
   a Russian text would actually use, not a mechanical letter-by-letter mapping.
5. second_name (the patronymic) means specifically that: a name derived from the father's
   given name, the way Russian and some other Slavic/Orthodox naming traditions use it. It is
   NOT a generic "whatever word sits in the middle" slot. Many naming traditions have no
   patronymic at all but do have more than one given name - Spanish "Pedro Luis González"
   (two given names, one surname, no patronymic), Portuguese, many Western double given names.
   When a name has multiple given-name-position words and none of them is recognizably a
   patronymic, put all of them in first_name/first_name_ru as one space-separated string and
   leave second_name_ru/second_name_en null - do not force a plausible-looking word into the
   patronymic slot just because of its position.
6. second_name_ru/second_name_en does not exist for everyone even among names that do use
   patronymics. Leave both null rather than inventing one - this is the one pair of fields
   allowed to legitimately stay empty, and rule 3's ban on invention applies to it most of all.
7. English spelling should be the natural/common form a person would actually use, not a
   mechanical letter-by-letter conversion - unless a matched candidate gives you the Russian
   form to transliterate, in which case transliterate that.

Reply with STRICT valid JSON only, no markdown, no text outside the JSON:
{{"matched_candidate": null,
  "surname_ru": null, "first_name_ru": null, "second_name_ru": null,
  "surname_en": null, "first_name_en": null, "second_name_en": null,
  "reason": "one short sentence"}}
"""


def _name_split_candidates(catalog: RussianNamesCatalog, person: Person) -> list[dict]:
    """Every catalog row sharing a folded surname with any known spelling of
    this person - a broad net (not the strict exact-key match
    RussianNamesCatalog.match() uses for identity/degree below), on purpose:
    the LLM does the namesake disambiguation strict match deliberately
    declines."""
    surnames = {
        _fold(token)
        for name in (person.name_raw, *person.name_variants)
        if name
        for token in name.replace(",", " ").split()
    }
    seen: set[tuple] = set()
    candidates: list[dict] = []
    for row in catalog.rows:
        if _fold(row.get("surname") or "") not in surnames:
            continue
        key = (row.get("surname"), row.get("name"), row.get("patronymic"))
        if key not in seen:
            seen.add(key)
            candidates.append(row)
    return candidates


def _build_name_split_prompt(person: Person, candidates: list[dict]) -> str:
    candidates_text = (
        "\n".join(
            f"{i}. surname={c.get('surname')!r} name={c.get('name')!r} "
            f"patronymic={c.get('patronymic') or ''!r} name_ru={c.get('name_ru') or ''!r}"
            for i, c in enumerate(candidates)
        )
        or "(no candidates with this surname in the directory)"
    )
    # Cyrillic/Latin homoglyphs ("А" vs "A") otherwise reach the model mixed
    # within one name - the same _unmix_alphabets pass matching uses above,
    # so the model isn't asked to read "I. А. Zelinskaya" with a stray
    # Cyrillic letter sitting inside a Latin name.
    variants = ", ".join(_unmix_alphabets(v) for v in person.name_variants) or "(none)"
    return NAME_SPLIT_PROMPT.format(
        name_raw=_unmix_alphabets(person.name_raw or ""),
        variants=variants,
        candidates=candidates_text,
    )


def _is_bare_initial(value: object) -> bool:
    """A name part that reduces to a single letter, with or without the
    trailing period rule 3 asks the model to omit - real replies carry one
    often enough (found across live test runs: "N.", "E.", "I.") that
    treating only the period-less form as an initial misses most of them.
    Either way it's a letter the model read and correctly refused to expand
    into a guess, per rule 3.
    """
    if not isinstance(value, str):
        return False
    stripped = value.strip().rstrip(".")
    return len(stripped) == 1 and stripped.isalpha()


def _plausibly_in_source(value: str, haystack: str) -> bool:
    """Whether a folded value is traceable to the source text.

    Full-string containment misses transliteration edges _fold does not
    resolve: Cyrillic "ь" folds to nothing while the natural English
    spelling of the same ending adds an "i" ("Анатольевна" folds to
    "anatolevna", "Anatolievna" folds to "anatolievna"). A shared stem of
    the first few folded characters is enough to tell a real
    transliteration from an invented one without chasing every such edge.
    """
    stem = _fold(value)
    return stem[:5] in haystack if len(stem) >= 3 else stem in haystack


def _guard_invented_second_name(person: Person, parsed: dict) -> dict:
    """Null a second_name (patronymic) the model invented from a bare
    initial, in place, for both languages.

    Models in this class (tested against qwen/qwen3.7-flash) break rule 3 on
    this roughly 1 in 10 times despite the explicit ban - e.g. turning
    "Valentine G. Nenajdenko" into second_name_en "Gennadievich" with the
    model's own reasoning admitting "not explicitly spelled out". A directory
    match (matched_candidate) is trusted per rule 1; anything else has to be
    traceable to the input - the same standard this module already applied
    when the logic was hand-written (see the module docstring: "guessing
    name parts from word order is not reliable enough to store").
    """
    if parsed.get("matched_candidate") is not None:
        return parsed
    haystack = _fold(f"{person.name_raw or ''} {' '.join(person.name_variants)}")
    for field in ("second_name_ru", "second_name_en"):
        value = parsed.get(field)
        if not value or _is_bare_initial(value) or _plausibly_in_source(value, haystack):
            continue
        parsed[field] = None
        parsed["reason"] = (
            f"{parsed.get('reason') or ''} [dropped invented {field}={value!r}: "
            "not in source text, no directory match]"
        ).strip()
    return parsed


_LATIN_LETTER = re.compile(r"[A-Za-z]")


def _guard_broken_transliteration(parsed: dict) -> dict:
    """Fix or null a *_ru field that isn't actually in Cyrillic, in place.

    Two different failures show up as "a Latin letter in a *_ru field":
    - A bare initial ("I", "A") is exactly what rule 3 allows a *_ru field
      to hold when only an initial is known - it is converted with
      to_cyrillic (the same table this module already uses), not discarded.
    - An unusual Latin character (a diacritic, e.g. Polish "ł") defeats the
      model mid-word instead of failing outright ("Małgorzata" comes back
      as "Маłgorzata" - the first two letters converted, the rest left
      as-is). Rule 4 requires these fields in full Cyrillic for every
      person - a field that isn't is worse than an empty one.
    """
    for field in ("surname_ru", "first_name_ru", "second_name_ru"):
        value = parsed.get(field)
        if not value or not _LATIN_LETTER.search(value):
            continue
        if _is_bare_initial(value):
            parsed[field] = to_cyrillic(value)
            continue
        parsed[field] = None
        parsed["reason"] = (
            f"{parsed.get('reason') or ''} [dropped broken {field}={value!r}: not fully Cyrillic]"
        ).strip()
    return parsed


# A patronymic suffix is not enough on its own to prove a word IS a
# patronymic (Бабич, Томкович and Ходасевич are surnames ending the same
# way), but it's enough to catch a word the model already put in the
# patronymic slot that doesn't even carry the shape - that's very likely a
# second given name forced into the wrong slot (Spanish "Pedro Luis
# González", Portuguese, Western double given names), the exact mistake
# rule 5 exists to head off.
_PATRONYMIC_LIKE_RU = re.compile(
    r"(ович|евич|ьевич|иевич|овна|евна|ьевна|иевна|инична|ична)$", re.IGNORECASE
)
_PATRONYMIC_LIKE_EN = re.compile(
    r"(ovich|evich|yevich|iyevich|ovna|evna|yevna|iyevna|inichna)$", re.IGNORECASE
)


def _guard_misclassified_second_name(parsed: dict) -> dict:
    """Fold a second_name back into first_name, in place, when it doesn't
    morphologically look like a patronymic and no candidate confirms it.

    _guard_invented_second_name only catches a value with no basis in the
    source text at all - it will not catch this, because the word IS
    genuinely part of the source, just extracted into the wrong field. A
    directory match is trusted per rule 1 (it can hand back an unusual but
    real patronymic). Otherwise, a non-initial value with no
    patronymic-shaped ending is folded back into first_name rather than
    left mislabeled - no data lost, just moved to where it belongs.
    """
    if parsed.get("matched_candidate") is not None:
        return parsed
    for second_field, first_field, pattern in (
        ("second_name_ru", "first_name_ru", _PATRONYMIC_LIKE_RU),
        ("second_name_en", "first_name_en", _PATRONYMIC_LIKE_EN),
    ):
        value = parsed.get(second_field)
        if not value or _is_bare_initial(value) or pattern.search(value):
            continue
        parsed[first_field] = f"{parsed.get(first_field) or ''} {value}".strip()
        parsed[second_field] = None
        parsed["reason"] = (
            f"{parsed.get('reason') or ''} [{second_field}={value!r} doesn't look like a "
            f"patronymic, folded into {first_field}]"
        ).strip()
    return parsed


class RussianNamesStage(EnrichmentStage):
    name = "russian_names"
    progress_label = "Authors: splitting names into RU/EN parts (LLM)"

    def run(self) -> dict[str, int]:
        catalog = RussianNamesCatalog.load(catalog_path(self.config))
        people = list(self.prepared.read_models("persons", Person))
        candidates = [
            person
            for person in people
            if self.selected("persons", person.id)
            and self.needs_attempt(person.processing.get(self.name))
        ]
        if not candidates:
            return {
                "russian_names": 0,
                "names_matched_candidate": 0,
                "names_second_name_corrected": 0,
                "names_failed": 0,
            }

        client = OpenRouterClient(
            self.config.request_timeout,
            self.config.openrouter_api_key,
            self.config.llm_model,
            self.config.openrouter_proxy_url,
        )
        llm_log = LlmLogStore(self.prepared.db, "llm_logs_russian_names")
        changed = matched = dropped = failed = 0
        for person in self.progress(candidates, total=len(candidates)):
            state = person.processing.get(self.name)
            if not person.name_raw:
                person.processing[self.name] = self._state(
                    state, ProcessingStatus.COMPLETED_EMPTY, 0
                )
                changed += 1
                continue

            # A free, deterministic lookup for the one field the LLM never
            # produces: an exact/initials catalog match names one employee
            # unambiguously, unlike the broader candidate net below, which
            # deliberately includes namesakes for the LLM to weigh.
            row = catalog.match(person)
            if row is not None:
                person.degree = person.degree or (row.get("degree") or "").strip() or None

            person_candidates = _name_split_candidates(catalog, person)
            prompt = _build_name_split_prompt(person, person_candidates)
            parsed = client.chat_json(prompt)
            llm_log.record(
                group=self.prepared.group,
                model=self.config.llm_model,
                prompt=prompt,
                raw_response=client.last_response,
                parsed=parsed,
                usage=client.last_usage,
                error=None if parsed is not None else "no response",
                context={"person_id": person.id},
            )
            if parsed is None:
                # Reverse transliteration for name_ru only, same as before
                # this stage called an LLM at all - guessing the parts from
                # word order is not reliable enough to store, so they stay
                # whatever an earlier successful run left them. Retried on
                # the next pipeline run (status FAILED).
                person.name_ru = person.name_ru or to_cyrillic(person.name_raw)
                person.processing[self.name] = self._state(
                    state, ProcessingStatus.FAILED, 0, error="llm request failed"
                )
                failed += 1
                changed += 1
                continue

            before_second_names = (parsed.get("second_name_ru"), parsed.get("second_name_en"))
            parsed = _guard_invented_second_name(person, parsed)
            parsed = _guard_misclassified_second_name(parsed)
            parsed = _guard_broken_transliteration(parsed)
            if (parsed.get("second_name_ru"), parsed.get("second_name_en")) != before_second_names:
                dropped += 1

            person.surname_ru = parsed.get("surname_ru") or None
            person.first_name_ru = parsed.get("first_name_ru") or None
            person.second_name_ru = parsed.get("second_name_ru") or None
            person.surname_en = parsed.get("surname_en") or None
            person.first_name_en = parsed.get("first_name_en") or None
            person.second_name_en = parsed.get("second_name_en") or None
            person.name_ru = (
                " ".join(
                    part
                    for part in (person.surname_ru, person.first_name_ru, person.second_name_ru)
                    if part
                )
                or person.name_ru
            )
            if parsed.get("matched_candidate") is not None:
                matched += 1

            person.processing[self.name] = self._state(state, ProcessingStatus.COMPLETED, 1)
            changed += 1

        if changed:
            self.prepared.write_models("persons", people)
        logger.info(
            "russian_names: %d processed, %d matched a directory candidate, "
            "%d second_name corrected (invented or misclassified), %d failed",
            changed,
            matched,
            dropped,
            failed,
        )
        return {
            "russian_names": changed,
            "names_matched_candidate": matched,
            "names_second_name_corrected": dropped,
            "names_failed": failed,
        }

    @staticmethod
    def _state(
        previous: ProcessingState | None,
        status: ProcessingStatus,
        result_count: int,
        error: str | None = None,
    ) -> ProcessingState:
        return ProcessingState(
            status=status,
            attempts=(previous.attempts if previous else 0) + 1,
            finished_at=datetime.now(UTC),
            result_count=result_count,
            error=error,
        )
