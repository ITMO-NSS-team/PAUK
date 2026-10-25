from __future__ import annotations

from copy import deepcopy
from datetime import date

from pauk.models import Funding, Publication, PublicationSourceRecord


def _record_rank(record: PublicationSourceRecord) -> tuple:
    publication_type = (record.type or "").casefold()
    type_rank = 0 if publication_type == "article" else 2 if publication_type == "preprint" else 1
    effective_date = record.publication_date
    if effective_date is None and record.year:
        effective_date = date(record.year, 12, 31)
    newest = -(effective_date or date.min).toordinal()
    completeness = -sum(bool(value) for value in (
        record.title, record.doi, record.journal, record.publication_date,
        record.abstract, record.pdf_urls, record.fields, record.authors,
    ))
    return type_rank, newest, completeness, record.key


def build_publication(entity_id: str, records: list[PublicationSourceRecord],
                      existing: Publication | None = None, *,
                      reset_enrichment: bool = True) -> Publication:
    """Deterministically project source records into the canonical view."""
    ranked = sorted(records, key=_record_rank)

    def first(name: str):
        return next((value for record in ranked if (value := getattr(record, name))), None)

    fields = list(dict.fromkeys(value for record in ranked for value in record.fields))
    pdf_urls = list(dict.fromkeys(value for record in ranked for value in record.pdf_urls))
    funding: list[Funding] = []
    for record in ranked:
        for value in record.metadata.get("funding") or []:
            item = Funding.model_validate(value)
            if item not in funding:
                funding.append(item)
    publication = Publication(
        id=entity_id,
        schema_version=2,
        title=first("title") or "Untitled",
        type=first("type"),
        fields=fields,
        journal=first("journal"),
        doi=first("doi"),
        publication_date=first("publication_date"),
        year=first("year"),
        pdf_urls=pdf_urls,
        abstract=first("abstract"),
        funding=funding,
        source_records=ranked,
    )
    if existing:
        known_identifiers = {identifier.normalized_key for identifier in publication.identifiers}
        for identifier in existing.identifiers:
            if identifier.normalized_key not in known_identifiers:
                publication.identifiers.append(deepcopy(identifier))
                known_identifiers.add(identifier.normalized_key)
        for name in (
            "has_code", "code_url", "full_text", "department_ids",
            "mentions_links", "merged_ids", "processing",
        ):
            setattr(publication, name, deepcopy(getattr(existing, name)))
        if reset_enrichment and records != existing.source_records:
            # Every publication enrichment consumes bibliographic data or
            # source PDF evidence, so it must be recomputed after a real
            # source-record change. An identical record never reaches here.
            publication.processing.clear()
            publication.full_text = None
            publication.mentions_links = []
            publication.has_code = False
            publication.code_url = None
    return publication
