from __future__ import annotations

import html
import logging
import re
from collections import OrderedDict
from datetime import date
from hashlib import sha1
from typing import TypeVar

from pauk.models import Authorship, Funding, Person, Publication
from pauk.storage import PreparedStore, RawStore

logger = logging.getLogger(__name__)

# Constrained (not bound) so a call passing Publication resolves _seed's
# return type to list[Publication], not list[Publication | Person].
_PreparedModel = TypeVar("_PreparedModel", Publication, Person)

ITMO_ROR_ID = "04txgxn49"

# Metadata sometimes puts something that is not a person in an author slot:
# ACL Anthology deposits name the venue there ("Association for Computational
# Linguistics 2026"), consortium papers name their collaborator group, and a
# submission form's contact field leaks through as the author's name
# ("vasilinetc.ira@gmail.com"). OpenAlex mints author entities for all of
# them; they are not people and never become persons. An address or a link is
# also a name no transliteration should ever be asked to render.
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
# "CaF <sub>2</sub>" — the subscript belongs to the formula before it.
SUBSUP_OPEN = re.compile(r"\s+(<(?:sub|sup)\b[^>]*>)", re.IGNORECASE)
# "Ag <sub>2</sub> B <sub>5</sub>" — an element symbol between two
# subscripts continues the same formula, unlike a following word.
SUBSUP_CHAIN = re.compile(r"(</(?:sub|sup)>)\s+(?=[A-Za-z]{1,2}\s*<(?:sub|sup)\b)", re.IGNORECASE)
TAG = re.compile(r"<[^>]+>")
SPACE_AFTER_OPEN = re.compile(r"([(\[])\s+")
SPACE_BEFORE_TIGHT = re.compile(r"\s+([/,;:.)\]@-])")
WHITESPACE = re.compile(r"\s+")


def _short_id(value: str | None) -> str | None:
    return value.rstrip("/").split("/")[-1] if value else None


def _math_text(match: re.Match) -> str:
    """The text of one MathML formula.

    Only leaf elements carry content, and the whitespace between them is
    layout rather than text, so leaves are concatenated with nothing in
    between: "<mml:mi>WSe</mml:mi> <mml:mn>2</mml:mn>" is "WSe2". A space
    that is a leaf of its own survives — that is how publishers encode the
    gap in "<mml:mi>a</mml:mi><mml:mi>b</mml:mi><mml:mo> </mml:mo>…",
    which reads "ab initio".
    """
    leaves = MATH_LEAF.findall(match.group(0))
    if not leaves:
        return WHITESPACE.sub("", TAG.sub("", match.group(0)))
    return "".join(text for _element, text in leaves)


def _clean_markup(text: str | None) -> str | None:
    """Drop the publisher markup OpenAlex passes through from Crossref.

    Physics and chemistry publishers deposit formulas as MathML or HTML
    (<sub>, <sup>) and species names in <i>, and OpenAlex serves the title
    verbatim — "monolayer <mml:math>…<mml:mi>WSe</mml:mi><mml:mn>2</mml:mn>…"
    where the title reads "monolayer WSe2". A formula collapses to its own
    text with the inner spacing removed and stays attached to what it
    subscripts; everything else just loses its tags.
    """
    if not text:
        return text
    cleaned = html.unescape(text)
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

    Fresh records arrive with author.id null but the display name — and
    often the ORCID — filled in. Keying on the OpenAlex id alone would drop
    those authorships and leave the publication with no authors at all, so
    they get a deterministic local id instead: the ORCID when there is one,
    otherwise a hash of the name, which keeps one node per distinct name.
    Either way the dedup stage can fold the person into the real author once
    OpenAlex assigns an id, by ORCID or by name.
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
    return [
        Funding(funder=grant.get("funder_display_name"), grant_id=grant.get("grant_id"))
        for grant in work.get("grants") or []
    ]


def _canonical_person_id(person: Person) -> str:
    """A person's id is their bare OpenAlex author ID.

    Older prepared files used affiliation-dependent ids ("itmo_A5X" /
    "external_A5X"), which split one author into two graph nodes whenever
    OpenAlex missed the ITMO affiliation on some of their works.
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
    base.name_variants = list(dict.fromkeys([*base.name_variants, *extra.name_variants]))
    base.department_ids = list(dict.fromkeys([*base.department_ids, *extra.department_ids]))
    base.merged_ids = list(dict.fromkeys([*base.merged_ids, *extra.merged_ids]))
    for authorship in extra.authored:
        if authorship not in base.authored:
            base.authored.append(authorship)
    for contribution in extra.contributed_to:
        if contribution not in base.contributed_to:
            base.contributed_to.append(contribution)
    for field in (
        "orcid",
        "name_en",
        "email",
        "first_name_ru",
        "second_name_ru",
        "surname_ru",
        "degree",
        "github",
        "google_scholar",
        "openreview",
        "thesis",
    ):
        if getattr(base, field) is None:
            setattr(base, field, getattr(extra, field))
    for stage, state in extra.processing.items():
        base.processing.setdefault(stage, state)
    return base


class OpenAlexNormalizer:
    def __init__(self, raw: RawStore, prepared: PreparedStore) -> None:
        self.raw = raw
        self.prepared = prepared

    def _seed(self, entity: str, model: type[_PreparedModel], ids: set[str]) -> list[_PreparedModel]:
        """This group's own rows for `entity`, plus any of `ids` it hasn't
        touched yet, looked up across every group.

        The group's own rows come first (and always) so a legacy/renamed
        row already sitting in this group's own state still gets
        canonicalized on every re-run, even when nothing in fresh raw data
        references it any more. The cross-group lookup is additive: a work
        or author already enriched by a different, overlapping run is
        found by id instead of re-created from scratch.
        """
        known = list(self.prepared.read_models(entity, model))
        missing = ids - {row.id for row in known}
        return [*known, *self.prepared.get_models(entity, missing, model)]

    def run(self) -> dict[str, int]:
        # Materialized once: both the id pre-scan below and the main loop
        # walk this same batch, and self.raw.read() is a one-shot cursor.
        envelopes = list(self.raw.read("openalex_works"))
        work_ids = {work_id for e in envelopes if (work_id := _short_id(e["payload"].get("id")))}
        person_ids = {
            person_id
            for e in envelopes
            for authorship in (e["payload"].get("authorships") or [])
            if (person_id := _authorship_person_id(authorship.get("author") or {}))
        }
        publications: OrderedDict[str, Publication] = OrderedDict(
            (row.id, row) for row in self._seed("publications", Publication, work_ids)
        )
        persons: OrderedDict[str, Person] = OrderedDict()
        for row in self._seed("persons", Person, person_ids):
            # A row an earlier, narrower filter let through (the check below
            # only knew about organizations, not contact addresses) is not a
            # person now either. Re-normalization is where the current rule
            # reaches rows already on disk.
            if NOT_A_PERSON_NAME.search(row.name_en or ""):
                logger.info("normalize: dropping an author row that is not a person: %s", row.name_en)
                continue
            canonical = _canonical_person_id(row)
            row.id = canonical
            existing = persons.get(canonical)
            persons[canonical] = _merge_person(existing, row) if existing else row
        # Authors previously folded by the dedup stage keep routing to their
        # canonical person on re-normalization instead of resurfacing as a
        # fresh duplicate row.
        merged_alias = {merged_id: person.id for person in persons.values() for merged_id in person.merged_ids}
        # Same for works the dedup stage folded into one publication: their
        # raw payloads are still on disk, but they are versions of a
        # publication that already exists, not publications of their own.
        publication_alias = {
            merged_id: publication.id for publication in publications.values() for merged_id in publication.merged_ids
        }
        for envelope in envelopes:
            work = envelope["payload"]
            work_id = _short_id(work.get("id"))
            if not work_id:
                continue
            publication_id = publication_alias.get(work_id, work_id)
            if publication_id == work_id:
                pub_date = work.get("publication_date")
                source = (work.get("primary_location") or {}).get("source") or {}
                normalized_publication = Publication(
                    id=work_id,
                    title=_clean_markup(work.get("title")) or "Untitled",
                    type=work.get("type"),
                    fields=_fields(work),
                    doi=work.get("doi"),
                    openalex_url=work.get("id"),
                    publication_date=date.fromisoformat(pub_date) if pub_date else None,
                    year=int(pub_date[:4]) if pub_date else None,
                    journal=source.get("display_name"),
                    pdf_url=(work.get("best_oa_location") or {}).get("pdf_url"),
                    abstract=_abstract(work),
                    funding=_funding(work),
                )
                existing_publication = publications.get(work_id)
                if existing_publication:
                    normalized_publication.has_code = existing_publication.has_code
                    normalized_publication.code_url = existing_publication.code_url
                    normalized_publication.department_ids = existing_publication.department_ids
                    normalized_publication.mentions_links = existing_publication.mentions_links
                    normalized_publication.versions = existing_publication.versions
                    normalized_publication.merged_ids = existing_publication.merged_ids
                    normalized_publication.processing = existing_publication.processing
                publications[work_id] = normalized_publication
            external_kept = 0
            for position, authorship in enumerate(work.get("authorships") or [], start=1):
                author = authorship.get("author") or {}
                name = author.get("display_name") or ""
                if NOT_A_PERSON_NAME.search(name):
                    logger.info("normalize: %s lists a non-person in an author slot, skipping: %s",
                                work_id, name)
                    continue
                openalex_id = _short_id(author.get("id"))
                person_id = openalex_id or _fallback_person_id(author)
                if not person_id:
                    continue
                person_id = merged_alias.get(person_id, person_id)
                institutions = authorship.get("institutions") or []
                is_itmo = any(ITMO_ROR_ID in (inst.get("ror") or inst.get("id") or "") for inst in institutions)
                if not is_itmo:
                    # Positions keep counting, so the kept authorships stay at
                    # the positions the work listed them under.
                    external_kept += 1
                    if external_kept > EXTERNAL_AUTHORS_LIMIT:
                        continue
                person = persons.setdefault(
                    person_id,
                    Person(
                        id=person_id,
                        openalex_id=openalex_id,
                        is_itmo=is_itmo,
                        name_en=author.get("display_name"),
                    ),
                )
                # At least one ITMO affiliation anywhere makes the person ITMO.
                person.is_itmo = person.is_itmo or is_itmo
                person.orcid = person.orcid or _short_id(author.get("orcid"))
                variants = author.get("display_name_alternatives") or []
                person.name_variants = list(dict.fromkeys([*person.name_variants, *variants]))
                authorship_record = Authorship(
                    publication_id=publication_id,
                    position=position,
                    affiliation="; ".join(authorship.get("raw_affiliation_strings") or []) or None,
                    is_corresponding=bool(authorship.get("is_corresponding")),
                )
                # Matched on (publication, position) rather than on equality:
                # one author legitimately appears twice on one work, but the
                # same slot must not come back as a second record just because
                # the persons stage filled in an affiliation the work omits.
                existing_authorship = next(
                    (
                        row
                        for row in person.authored
                        if row.publication_id == publication_id and row.position == position
                    ),
                    None,
                )
                if existing_authorship is None:
                    person.authored.append(authorship_record)
                    continue
                existing_authorship.is_corresponding = authorship_record.is_corresponding
                if authorship_record.affiliation:
                    # What the work states always wins over a filled-in value.
                    existing_authorship.affiliation = authorship_record.affiliation
                    existing_authorship.affiliation_source = None
        self.prepared.write_models("publications", publications.values())
        self.prepared.write_models("persons", persons.values())
        return {"publications": len(publications), "persons": len(persons)}
