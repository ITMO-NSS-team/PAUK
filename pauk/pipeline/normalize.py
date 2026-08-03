from __future__ import annotations

from collections import OrderedDict
from datetime import date
from hashlib import sha1

from pauk.models import Authorship, Funding, Person, Publication
from pauk.storage import GroupLock, PreparedStore, RawStore

ITMO_ROR_ID = "04txgxn49"


def _short_id(value: str | None) -> str | None:
    return value.rstrip("/").split("/")[-1] if value else None


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
    return " ".join(word for _, word in words)


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
    for field in ("orcid", "name_en", "email", "first_name_ru", "second_name_ru",
                  "surname_ru", "degree", "github", "google_scholar", "openreview", "thesis"):
        if getattr(base, field) is None:
            setattr(base, field, getattr(extra, field))
    for stage, state in extra.processing.items():
        base.processing.setdefault(stage, state)
    return base


class OpenAlexNormalizer:
    def __init__(self, raw: RawStore, prepared: PreparedStore) -> None:
        self.raw = raw
        self.prepared = prepared

    def run(self) -> dict[str, int]:
        with GroupLock(self.prepared.group_dir.parent.parent, self.prepared.group_dir.name):
            return self._run()

    def _run(self) -> dict[str, int]:
        publications: OrderedDict[str, Publication] = OrderedDict(
            (row.id, row) for row in self.prepared.read_models("publications", Publication)
        )
        persons: OrderedDict[str, Person] = OrderedDict()
        for row in self.prepared.read_models("persons", Person):
            canonical = _canonical_person_id(row)
            row.id = canonical
            existing = persons.get(canonical)
            persons[canonical] = _merge_person(existing, row) if existing else row
        # Authors previously folded by the dedup stage keep routing to their
        # canonical person on re-normalization instead of resurfacing as a
        # fresh duplicate row.
        merged_alias = {
            merged_id: person.id
            for person in persons.values()
            for merged_id in person.merged_ids
        }
        # Same for works the dedup stage folded into one publication: their
        # raw payloads are still on disk, but they are versions of a
        # publication that already exists, not publications of their own.
        publication_alias = {
            merged_id: publication.id
            for publication in publications.values()
            for merged_id in publication.merged_ids
        }
        for envelope in self.raw.read("openalex_works"):
            work = envelope["payload"]
            work_id = _short_id(work.get("id"))
            if not work_id:
                continue
            publication_id = publication_alias.get(work_id, work_id)
            if publication_id == work_id:
                pub_date = work.get("publication_date")
                source = ((work.get("primary_location") or {}).get("source") or {})
                normalized_publication = Publication(
                    id=work_id,
                    title=work.get("title") or "Untitled",
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
            for position, authorship in enumerate(work.get("authorships") or [], start=1):
                author = authorship.get("author") or {}
                openalex_id = _short_id(author.get("id"))
                person_id = openalex_id or _fallback_person_id(author)
                if not person_id:
                    continue
                person_id = merged_alias.get(person_id, person_id)
                institutions = authorship.get("institutions") or []
                is_itmo = any(ITMO_ROR_ID in (inst.get("ror") or inst.get("id") or "") for inst in institutions)
                person = persons.setdefault(person_id, Person(
                    id=person_id,
                    openalex_id=openalex_id,
                    is_itmo=is_itmo,
                    name_en=author.get("display_name"),
                ))
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
                if authorship_record not in person.authored:
                    person.authored.append(authorship_record)
        self.prepared.write_models("publications", publications.values())
        self.prepared.write_models("persons", persons.values())
        return {"publications": len(publications), "persons": len(persons)}
