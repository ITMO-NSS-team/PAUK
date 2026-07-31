from __future__ import annotations

from collections import OrderedDict
from datetime import date

from pauk.models import Authorship, Funding, Person, Publication
from pauk.storage import GroupLock, PreparedStore, RawStore

ITMO_ROR_ID = "04txgxn49"


def _short_id(value: str | None) -> str | None:
    return value.rstrip("/").split("/")[-1] if value else None


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
        persons: OrderedDict[str, Person] = OrderedDict(
            (row.id, row) for row in self.prepared.read_models("persons", Person)
        )
        for envelope in self.raw.read("openalex_works"):
            work = envelope["payload"]
            work_id = _short_id(work.get("id"))
            if not work_id:
                continue
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
                normalized_publication.processing = existing_publication.processing
            publications[work_id] = normalized_publication
            for position, authorship in enumerate(work.get("authorships") or [], start=1):
                author = authorship.get("author") or {}
                author_id = _short_id(author.get("id"))
                if not author_id:
                    continue
                institutions = authorship.get("institutions") or []
                is_itmo = any(ITMO_ROR_ID in (inst.get("ror") or inst.get("id") or "") for inst in institutions)
                person_id = f"itmo_{author_id}" if is_itmo else f"external_{author_id}"
                person = persons.setdefault(person_id, Person(
                    id=person_id,
                    openalex_id=author_id,
                    is_itmo=is_itmo,
                    name_en=author.get("display_name"),
                ))
                variants = author.get("display_name_alternatives") or []
                person.name_variants = list(dict.fromkeys([*person.name_variants, *variants]))
                authorship_record = Authorship(
                    publication_id=work_id,
                    position=position,
                    affiliation="; ".join(authorship.get("raw_affiliation_strings") or []) or None,
                    is_corresponding=bool(authorship.get("is_corresponding")),
                )
                if authorship_record not in person.authored:
                    person.authored.append(authorship_record)
        self.prepared.write_models("publications", publications.values())
        self.prepared.write_models("persons", persons.values())
        return {"publications": len(publications), "persons": len(persons)}
