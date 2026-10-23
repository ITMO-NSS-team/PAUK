"""The university a deployment collects: everything that differs between ITMO and UFJF.

One profile per university. Code outside this module asks the active profile
(`active()`, chosen by PAUK_INSTITUTION) instead of naming a university, so a
new one is data, not a fork: `data/static/<key>/profile.json` with the fields
of `Institution`, next to its department catalogue. ITMO and UFJF are written
out below because their tests and docs refer to them.

The "local" language is the one the university's own names are written in:
Russian for ITMO, Portuguese for UFJF. Name fields ending in `_local` hold that
language; `_en` fields hold English.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, fields
from functools import cached_property
from pathlib import Path

STATIC_ROOT = Path(__file__).resolve().parent.parent / "data" / "static"


@dataclass(frozen=True)
class Institution:
    key: str
    short_name: str
    short_name_local: str
    name_en: str
    name_local: str
    ror_id: str
    # Language of the `_local` name fields, as the LLM prompt names it.
    language: str
    # Persons' local names are Cyrillic (ITMO) or Latin (UFJF); the author_names
    # stage checks the reply against it.
    local_script: str
    # Only naming traditions with a patronymic fill second_name_*.
    has_patronymic: bool
    # Particles that belong to the given names in a citation ("Silva, José
    # Carlos da"); a surname the model starts with one is moved back.
    surname_particles: tuple[str, ...]
    # Bare domains; subdomains (estudante.ufjf.br) count as the same host.
    email_domains: tuple[str, ...]
    # The university named as a word in affiliation text.
    marker: str
    # Spelled-out names, folded to `marker_replacement` before parts are
    # split on commas: "Information Technologies, Mechanics" would otherwise
    # land in two parts and neither would carry the marker.
    long_name: str
    marker_replacement: str
    # Address words: a part made only of these says nothing about a unit.
    place_words: str
    # Neighbouring organisations that own a unit name when they sit next to it,
    # on top of the generic "university/academy/hospital" words.
    other_organisations: str
    # The university named in a GitHub profile or organization: strong enough
    # to make an organization a social_graph seed.
    identity: str
    # The city in a GitHub profile: only a weak person-matching signal.
    city: str
    # Official staff directory (personal data, never committed), read from the
    # static dir. None: the university has none, and author_names runs without it.
    staff_catalog: str | None
    mongo_db: str
    site_url: str
    # Words that name the university's own units although they usually name
    # an organisation of their own: "College of Computer Science" at Fudan,
    # "Trinity College" at Cambridge. A part holding one is not taken for a
    # neighbour.
    unit_words: str = ""
    # External coauthors in the graph. Without them they stay light rows in
    # Mongo (name and OpenAlex id, as normalize makes them) so that dedup keeps
    # its shared-coauthor evidence, but nothing enriches, names or publishes them.
    external_authors: bool = False

    @cached_property
    def unit_words_re(self) -> re.Pattern[str] | None:
        return re.compile(self.unit_words, re.IGNORECASE) if self.unit_words else None

    @cached_property
    def marker_re(self) -> re.Pattern[str]:
        return re.compile(self.marker, re.IGNORECASE)

    @cached_property
    def long_name_re(self) -> re.Pattern[str]:
        return re.compile(self.long_name, re.IGNORECASE)

    @cached_property
    def identity_re(self) -> re.Pattern[str]:
        return re.compile(self.identity, re.IGNORECASE)

    @cached_property
    def city_re(self) -> re.Pattern[str]:
        return re.compile(self.city, re.IGNORECASE)

    def is_member_ror(self, ror_or_id: str | None) -> bool:
        """Whether an OpenAlex institution's `ror`/`id`, or a bare ROR id, is this university."""
        return bool(ror_or_id) and self.ror_id in ror_or_id

    def is_institutional_email(self, email: str) -> bool:
        host = email.rpartition("@")[2].casefold()
        return any(host == domain or host.endswith("." + domain) for domain in self.email_domains)


ITMO = Institution(
    key="itmo",
    short_name="ITMO",
    short_name_local="ИТМО",
    name_en="ITMO University",
    name_local="Университет ИТМО",
    ror_id="04txgxn49",
    language="Russian",
    local_script="cyrillic",
    has_patronymic=True,
    surname_particles=(),
    email_domains=("itmo.ru", "ifmo.ru"),
    # Digits are fine on either side ("1ITMO"), and so is "ITMOUniversity".
    marker=r"(?<![^\W\d_])(?:(?:itmo|ifmo|итмо)(?![^\W\d_])|itmo(?=universit))",
    long_name=r"information\s+technolog\w*\s*,?\s*mechanics|информационных\s+технологий\s*,?\s*механики",
    marker_replacement="ITMO mechanics",
    place_words=(
        r"saint|sankt|st|petersburg|peterburg|spb|russia|russian|federation|fed|ru|санкт|петербург|россия"
        r"|российская|федерация|рф|г|kronver\w*|кронверкск\w*|pr|пр|т|prospekt|prospect|prosp|проспект"
        r"|lomonosov\w*|ломоносова|birzhevaya|биржевая|liniya|линия|line|kadetskaya|grivtsova|chaikovskogo"
        r"|ul|ул|улица|per|lit|лит|а|д"
    ),
    other_organisations=r"\b(?:spbu|spbsu|спбгу|spbpu|spbstu|спбпу|msu|мгу|mipt|мфти|hse|вшэ|leti|лэти)\b",
    # "RITMO, University of Oslo" is a Norwegian centre whose name contains the
    # same four letters, hence the word boundaries.
    identity=r"\b(?:itmo|итмо)\b",
    # A bare "Sankt" accepts unrelated profile text; Petersburg is required too.
    city=r"\b(?:saint|st\.?|sankt)[-\s]*peters?burg\b|\bсанкт[-\s]?петербург",
    staff_catalog="russian_names.csv",
    mongo_db="pauk",
    site_url="https://itmo.ru",
    # The ITMO map shows external coauthors behind a filter.
    external_authors=True,
)

UFJF = Institution(
    key="ufjf",
    short_name="UFJF",
    short_name_local="UFJF",
    name_en="Federal University of Juiz de Fora",
    name_local="Universidade Federal de Juiz de Fora",
    ror_id="04yqw9c44",
    language="Brazilian Portuguese",
    local_script="latin",
    has_patronymic=False,
    surname_particles=("da", "de", "do", "das", "dos"),
    email_domains=("ufjf.br", "ufjf.edu.br"),
    marker=r"(?<![^\W\d_])ufjf(?![^\W\d_])",
    # Web of Science abbreviates to "Univ Fed Juiz de Fora"; authors also write
    # "Juíz" and "Juiz de Fora Federal University".
    long_name=(
        r"(?:universidade|univ\.?)\s+fed(?:eral|\.)?\s+(?:de\s+)?ju[ií]z\s+de\s+fora"
        r"|federal\s+univ(?:ersity|\.)?\s+(?:of\s+)?ju[ií]z\s+de\s+fora"
        r"|ju[ií]z\s+de\s+fora\s+federal\s+univ(?:ersity|\.)?"
    ),
    marker_replacement="UFJF",
    # "Governador Valadares" is left out: it names the second campus, a unit.
    place_words=(
        r"ju[ií]z|de|fora|mg|minas|gerais|brazil|brasil|br|rua|r|avenida|campus|universit[aá]rio|bairro|s|n|sn"
        r"|cep|s[aã]o|pedro|jos[eé]|louren[cç]o|kelmer|martelos|centro|dr|raimundo|monteiro|rezende|do|da|dos|das|e"
    ),
    # The university hospital (HU-UFJF, run by EBSERH) is a separate ROR the
    # profile leaves out; Embrapa Dairy Cattle and UniAcademia share the city.
    other_organisations=(
        r"\b(?:usp|unicamp|unesp|unifesp|ufmg|ufrj|uff|ufv|ufsj|ufop|ufla|ufes|uerj|cefet|ebserh|embrapa"
        r"|fiocruz|uniacademia|suprema|hu)\b|instituto\s+federal|centro\s+universit[aá]rio|santa\s+casa"
        r"|faculdade\s+de\s+ci[eê]ncias\s+m[eé]dicas"
    ),
    identity=r"\bufjf\b|universidade\s+federal\s+de\s+juiz\s+de\s+fora|federal\s+university\s+of\s+juiz\s+de\s+fora",
    city=r"\bjuiz\s+de\s+fora\b",
    staff_catalog=None,
    mongo_db="pauk_ufjf",
    site_url="https://www2.ufjf.br",
)

PROFILES = {profile.key: profile for profile in (ITMO, UFJF)}


def load(path: Path) -> Institution:
    """A profile from its JSON file; unknown keys are an error, so a typo does not pass silently."""
    data = json.loads(path.read_text(encoding="utf-8"))
    known = {field.name for field in fields(Institution)}
    if unknown := set(data) - known:
        raise ValueError(f"{path}: unknown profile fields {sorted(unknown)}")
    data["email_domains"] = tuple(data.get("email_domains") or ())
    data["surname_particles"] = tuple(data.get("surname_particles") or ())
    return Institution(**data)


def get(key: str) -> Institution:
    key = key.strip().casefold()
    if key in PROFILES:
        return PROFILES[key]
    path = STATIC_ROOT / key / "profile.json"
    if path.exists():
        return load(path)
    known = sorted({*PROFILES, *(p.parent.name for p in STATIC_ROOT.glob("*/profile.json"))})
    raise ValueError(f"unknown institution {key!r}; known: {', '.join(known)}")


def active() -> Institution:
    """The profile this process runs for (PAUK_INSTITUTION, ITMO by default)."""
    return get(os.getenv("PAUK_INSTITUTION") or ITMO.key)
