from __future__ import annotations

import html
import re
from copy import deepcopy
from hashlib import sha1

from pauk.models import Funding, Person
from pauk.storage import PreparedStore, RawStore

ITMO_ROR_ID = "04txgxn49"

# An author slot sometimes holds a venue, a collaborator group or a contact
# address. OpenAlex mints author entities for all of them; persons they are not.
NOT_A_PERSON_NAME = re.compile(
    r"\b(association|conference|proceedings|workshop|committee|consortium"
    r"|collaborat\w*|society)\b"
    r"|\S+@\S+"
    r"|\bhttps?://|\bwww\.",
    re.IGNORECASE,
)

# Consortium papers carry hundreds of authors. All ITMO-affiliated ones are
# kept; external coauthors are capped so that a single epidemiology
# consortium does not flood the graph.
EXTERNAL_AUTHORS_LIMIT = 500

MATH_BLOCK = re.compile(r"<mml:math\b[^>]*>.*?</mml:math>", re.DOTALL | re.IGNORECASE)
MATH_LEAF = re.compile(r"<mml:(mi|mn|mo|mtext)\b[^>]*>(.*?)</mml:\1>", re.DOTALL | re.IGNORECASE)
# "CaF <sub>2</sub>": the subscript belongs to the formula before it.
SUBSUP_OPEN = re.compile(r"\s+(<(?:sub|sup)\b[^>]*>)", re.IGNORECASE)
# "Ag <sub>2</sub> B <sub>5</sub>": an element symbol between two
# subscripts continues the same formula, unlike a following word.
SUBSUP_CHAIN = re.compile(r"(</(?:sub|sup)>)\s+(?=[A-Za-z]{1,2}\s*<(?:sub|sup)\b)", re.IGNORECASE)
# "CaCO-=SUB=-3-=/SUB=-": elibrary-style markup that some Russian journals
# deposit instead of HTML. Rewritten into tags so the rules above apply to it.
PSEUDO_TAG = re.compile(r"-=(/?)([A-Za-z][A-Za-z0-9]*)=-")
TAG = re.compile(r"<[^>]+>")
SPACE_AFTER_OPEN = re.compile(r"([(\[])\s+")
SPACE_BEFORE_TIGHT = re.compile(r"\s+([/,;:.)\]@-])")
WHITESPACE = re.compile(r"\s+")


def _short_id(value: str | None) -> str | None:
    return value.rstrip("/").split("/")[-1] if value else None


def _math_text(match: re.Match) -> str:
    """The text of one MathML formula.

    Only leaf elements carry content and whitespace between them is layout,
    so leaves are concatenated directly: "<mml:mi>WSe</mml:mi> <mml:mn>2</mml:mn>"
    is "WSe2". A space that is a leaf of its own survives; publishers use that
    to encode the gap in "ab initio".
    """
    leaves = MATH_LEAF.findall(match.group(0))
    if not leaves:
        return WHITESPACE.sub("", TAG.sub("", match.group(0)))
    return "".join(text for _element, text in leaves)


def _clean_markup(text: str | None) -> str | None:
    """Drop the publisher markup OpenAlex passes through from Crossref.

    Publishers deposit formulas as MathML or HTML (<sub>, <sup>) and OpenAlex
    serves the title verbatim, e.g. "monolayer <mml:math>...</mml:math>" where
    the title reads "monolayer WSe2". A formula collapses to its own text with
    inner spacing removed and stays attached to what it subscripts; everything
    else just loses its tags. elibrary markup is normalized to tags first.
    """
    if not text:
        return text
    cleaned = html.unescape(text)
    cleaned = PSEUDO_TAG.sub(r"<\1\2>", cleaned)
    cleaned = MATH_BLOCK.sub(_math_text, cleaned)
    cleaned = SUBSUP_CHAIN.sub(r"\1", cleaned)
    cleaned = SUBSUP_OPEN.sub(r"\1", cleaned)
    cleaned = TAG.sub("", cleaned)
    cleaned = SPACE_AFTER_OPEN.sub(r"\1", cleaned)
    cleaned = SPACE_BEFORE_TIGHT.sub(r"\1", cleaned)
    return " ".join(cleaned.split())


def _authorship_person_id(author: dict) -> str | None:
    return _short_id(author.get("id")) or _fallback_person_id(author)


def _fallback_person_id(author: dict) -> str | None:
    """Local identity for an author OpenAlex has not disambiguated yet.

    Fresh records arrive with author.id null but the display name (and often
    the ORCID) filled in. Keying on the OpenAlex id alone would drop those
    authorships, so they get a deterministic local id: the ORCID when there is
    one, otherwise a hash of the name. The dedup stage can later fold the
    person into the real author by ORCID or name.
    """
    orcid = _short_id(author.get("orcid"))
    if orcid:
        return f"orcid_{orcid}"
    name = " ".join((author.get("display_name") or "").split())
    if not name:
        return None
    return f"name_{sha1(name.casefold().encode()).hexdigest()[:12]}"


def _abstract(work: dict) -> str | None:
    inverted = work.get("abstract_inverted_index") or {}
    if not inverted:
        return None
    words = sorted(((position, word) for word, positions in inverted.items() for position in positions))
    return _clean_markup(" ".join(word for _, word in words))


def _fields(work: dict) -> list[str]:
    """The OpenAlex topic fields a work belongs to, deduplicated."""
    return list(
        dict.fromkeys(
            field for topic in work.get("topics") or [] if (field := ((topic.get("field") or {}).get("display_name")))
        )
    )


def _funding(work: dict) -> list[Funding]:
    funders = work.get("funders") or []
    entries: dict[tuple[str | None, str | None], Funding] = {}
    represented_ids = set()
    for award in work.get("awards") or []:
        funder_id = award.get("funder_id")
        name = award.get("funder_display_name") or None
        grant_id = award.get("funder_award_id") or None
        if name or grant_id:
            entries.setdefault((name, grant_id), Funding(funder=name, grant_id=grant_id))
            if funder_id:
                represented_ids.add(funder_id)
    # Funders without an award still describe funding, but an award's funder
    # must not also produce a redundant entry with an unknown grant number.
    for funder in funders:
        name = funder.get("display_name")
        if name and funder.get("id") not in represented_ids:
            entries.setdefault((name, None), Funding(funder=name))
    # A funder-only record adds no information when its numbered grants
    # are already present, regardless of their order in the source.
    named_grant_funders = {name for name, grant_id in entries if name and grant_id}
    return [entry for (name, grant_id), entry in entries.items() if grant_id or name not in named_grant_funders]


def _canonical_person_id(person: Person) -> str:
    """A person's id is their bare OpenAlex author ID.

    Legacy prepared files used affiliation-dependent ids ("itmo_A5X" /
    "external_A5X"), which split one author into two graph nodes.
    """
    return person.openalex_id or person.id.removeprefix("itmo_").removeprefix("external_")


def _merge_person(base: Person, extra: Person) -> Person:
    """Merge two prepared rows describing the same author (legacy split ids).

    is_itmo is an OR (at least one ITMO affiliation makes the person ITMO);
    lists are deduplicated unions; scalar fields keep base's value and fill
    gaps from extra; processing states are kept from base, missing stages
    come from extra.
    """
    base.is_itmo = base.is_itmo or extra.is_itmo
    for field in Person.model_fields:
        if field in {"id", "openalex_id", "is_itmo", "processing"}:
            continue
        current, incoming = getattr(base, field), getattr(extra, field)
        if isinstance(current, list):
            for value in incoming:
                if value not in current:
                    current.append(deepcopy(value))
        elif current is None and incoming is not None:
            setattr(base, field, incoming)
    for address in (base.email, extra.email):
        if address and address not in base.emails:
            base.emails.append(address)
    for stage, state in extra.processing.items():
        base.processing.setdefault(stage, state)
    return base


class OpenAlexNormalizer:
    """Compatibility facade for the source-neutral normalization pipeline."""

    def __init__(self, raw: RawStore, prepared: PreparedStore) -> None:
        self.raw = raw
        self.prepared = prepared

    def run(self) -> dict[str, int]:
        from pauk.ingest.normalizer import PublicationNormalizer

        return PublicationNormalizer(self.raw, self.prepared).run("openalex")
