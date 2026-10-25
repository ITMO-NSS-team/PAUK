from __future__ import annotations

import logging
from collections import OrderedDict
from copy import deepcopy

from pauk.models import (
    Authorship,
    ExternalIdentifier,
    Person,
    Publication,
    PublicationSourceRecord,
    SourceAuthor,
)
from pauk.storage import PreparedStore, RawStore
from pauk.storage.review import record_publication_pair

from .aggregate import build_publication
from .contracts import IncomingPublication, RawEnvelope
from .identity import IdentityResolver
from .registry import AdapterRegistry, built_in_registry

logger = logging.getLogger(__name__)


class PublicationNormalizer:
    """Adapter -> identity -> aggregate -> prepared-store orchestrator."""

    def __init__(self, raw: RawStore, prepared: PreparedStore,
                 registry: AdapterRegistry | None = None) -> None:
        self.raw = raw
        self.prepared = prepared
        self.registry = registry or built_in_registry()
        self.identities = IdentityResolver(prepared.db)

    def _incoming(self, source: str) -> list[tuple[RawEnvelope, IncomingPublication]]:
        latest: OrderedDict[str, tuple[RawEnvelope, IncomingPublication]] = OrderedDict()
        # Adapt every row before identity resolution writes anything. An
        # unknown provider or malformed payload therefore fails atomically
        # with respect to prepared data and identity keys.
        for row in self.raw.read_provider(source):
            envelope = RawEnvelope.model_validate(row)
            adapter = self.registry.get(envelope.provider)
            incoming = adapter.adapt(envelope)
            previous = latest.get(incoming.record_key)
            if previous and envelope.schema_version < 2:
                previous_authors = {
                    (author.source_author_id, author.name, author.position): author
                    for author in previous[1].authors
                }
                for author in incoming.authors:
                    old = previous_authors.get((author.source_author_id, author.name, author.position))
                    if old:
                        author.is_corresponding = old.is_corresponding or author.is_corresponding
            latest[incoming.record_key] = (envelope, incoming)
        return list(latest.values())

    def _bootstrap(self, publications: OrderedDict[str, Publication],
                   persons: OrderedDict[str, Person]) -> None:
        """Teach identity_keys about legacy rows without renumbering them."""
        for publication in publications.values():
            identifiers = list(publication.identifiers)
            if publication.id.startswith("W"):
                identifiers.append(ExternalIdentifier(
                    scheme="openalex", value=publication.id,
                    url=publication.openalex_url,
                ))
            for merged_id in publication.merged_ids:
                if merged_id.startswith("W"):
                    identifiers.append(ExternalIdentifier(scheme="openalex", value=merged_id))
            for identifier in identifiers:
                if identifier.scheme == "doi":
                    # Historical duplicates are assigned in migration using
                    # merged_ids/rank; bootstrapping must not choose early.
                    continue
                owner = self.identities.keys.claim("publication", identifier, publication.id)
                if owner != publication.id:
                    logger.warning("identity key %s already belongs to %s", identifier.normalized_key, owner)
        canonical_people: OrderedDict[str, Person] = OrderedDict()
        for person in persons.values():
            from pauk.pipeline.normalize import NOT_A_PERSON_NAME

            if NOT_A_PERSON_NAME.search(person.name_raw or ""):
                continue
            canonical_id = person.openalex_id or person.id.removeprefix("itmo_").removeprefix("external_")
            person.id = canonical_id
            existing = canonical_people.get(canonical_id)
            if existing:
                existing.is_itmo = existing.is_itmo or person.is_itmo
                for name in Person.model_fields:
                    if name in {"id", "openalex_id", "is_itmo", "processing"}:
                        continue
                    current = getattr(existing, name)
                    incoming = getattr(person, name)
                    if isinstance(current, list):
                        current.extend(item for item in incoming if item not in current)
                    elif current is None and incoming is not None:
                        setattr(existing, name, deepcopy(incoming))
                for stage, state in person.processing.items():
                    existing.processing.setdefault(stage, state)
            else:
                canonical_people[canonical_id] = person
        persons.clear()
        persons.update(canonical_people)
        for person in persons.values():
            identifiers = list(person.identifiers)
            if person.openalex_id:
                identifiers.append(ExternalIdentifier(scheme="openalex", value=person.openalex_id))
            if person.orcid:
                identifiers.append(ExternalIdentifier(scheme="orcid", value=person.orcid))
            identifiers.extend(
                ExternalIdentifier(scheme="openalex", value=merged_id)
                for merged_id in person.merged_ids if merged_id.startswith("A")
            )
            person.identifiers = list({item.normalized_key: item for item in identifiers}.values())
            for identifier in person.identifiers:
                owner = self.identities.keys.claim("person", identifier, person.id)
                if owner != person.id:
                    logger.warning("identity key %s already belongs to %s", identifier.normalized_key, owner)

    def run(self, source: str = "openalex") -> dict[str, int]:
        batches = self._incoming(source)
        publications: OrderedDict[str, Publication] = OrderedDict(
            (row.id, row) for row in self.prepared.read_models("publications", Publication)
        )
        persons: OrderedDict[str, Person] = OrderedDict(
            (row.id, row) for row in self.prepared.read_models("persons", Person)
        )
        self._bootstrap(publications, persons)
        affected: set[str] = set()

        for envelope, incoming in batches:
            publication_id = self.identities.publication_id(
                incoming, legacy=envelope.schema_version < 2,
            )
            publication_id = next(
                (row.id for row in publications.values() if publication_id in row.merged_ids),
                publication_id,
            )
            existing = publications.get(publication_id) or self.identities.publication(publication_id)
            if existing:
                publications[publication_id] = existing
            previous_record = next(
                (record for record in existing.source_records if record.key == incoming.record_key),
                None,
            ) if existing else None

            source_authors: list[SourceAuthor] = []
            for author in incoming.authors:
                person_id = self.identities.person_id(
                    author, incoming.source, incoming.record_key,
                    legacy=envelope.schema_version < 2,
                )
                person_id = next(
                    (row.id for row in persons.values() if person_id in row.merged_ids),
                    person_id,
                )
                person = persons.get(person_id) or self.identities.person(person_id) or Person(
                    id=person_id,
                    schema_version=2,
                    openalex_id=author.source_author_id if incoming.source == "openalex" else None,
                    is_itmo=author.is_itmo,
                    name_raw=author.name,
                )
                previous_author = next(
                    (
                        item for item in previous_record.authors
                        if item.position == author.position and item.person_id == person_id
                    ),
                    None,
                ) if previous_record else None
                person.schema_version = max(person.schema_version, 2)
                person.is_itmo = person.is_itmo or author.is_itmo
                source_author_changed = previous_author is not None and any((
                    previous_author.name != author.name,
                    previous_author.affiliation != author.affiliation,
                    previous_author.is_corresponding != author.is_corresponding,
                    previous_author.identifiers != author.identifiers,
                ))
                if author.name and (person.name_raw is None or source_author_changed):
                    person.name_raw = author.name
                if source_author_changed and envelope.schema_version >= 2:
                    person.processing.clear()
                person.orcid = person.orcid or author.orcid
                person.name_variants = list(dict.fromkeys([*person.name_variants, *author.name_variants]))
                person.identifiers = list({
                    item.normalized_key: item
                    for item in [*person.identifiers, *author.identifiers]
                }.values())
                persons[person_id] = person
                source_authors.append(SourceAuthor(
                    person_id=person_id,
                    name=author.name,
                    position=author.position,
                    identifiers=author.identifiers,
                    affiliation=author.affiliation,
                    is_corresponding=author.is_corresponding,
                ))
            record = PublicationSourceRecord(
                key=incoming.record_key,
                source=incoming.source,
                source_id=incoming.source_id,
                source_url=incoming.source_url,
                title=incoming.title,
                type=incoming.type,
                fields=incoming.fields,
                journal=incoming.journal,
                doi=incoming.doi,
                publication_date=incoming.publication_date,
                year=incoming.year,
                pdf_urls=incoming.pdf_urls,
                abstract=incoming.abstract,
                authors=source_authors,
                metadata={
                    **incoming.metadata,
                    "funding": [item.model_dump(mode="json") for item in incoming.funding],
                },
            )
            records = [
                deepcopy(item) for item in (existing.source_records if existing else [])
                if item.key != record.key
            ]
            if envelope.schema_version < 2 and existing:
                record.pdf_urls = list(dict.fromkeys([*record.pdf_urls, *existing.pdf_urls]))
            records.append(record)
            publications[publication_id] = build_publication(
                publication_id, records, existing,
                reset_enrichment=envelope.schema_version >= 2,
            )
            affected.add(publication_id)

        # Rebuild authorships for affected publications from their complete
        # provenance ledger, including authors supplied by another source.
        if affected:
            for row in self.prepared.db.persons.find(
                {"authored.publication_id": {"$in": list(affected)}},
                {"_id": False, "groups": False, "_version": False},
            ):
                person = Person.model_validate(row)
                from pauk.pipeline.normalize import NOT_A_PERSON_NAME

                if NOT_A_PERSON_NAME.search(person.name_raw or ""):
                    continue
                persons.setdefault(person.id, person)
            for person in persons.values():
                person.authored = [item for item in person.authored if item.publication_id not in affected]
            for publication_id in sorted(affected):
                publication = publications[publication_id]
                for record in publication.source_records:
                    for author in record.authors:
                        person = persons.get(author.person_id) or self.identities.person(author.person_id)
                        if person is None:
                            continue
                        persons[person.id] = person
                        authorship = Authorship(
                            publication_id=publication_id,
                            position=author.position or 0,
                            affiliation=author.affiliation,
                            is_corresponding=author.is_corresponding,
                        )
                        if not any(
                            item.publication_id == publication_id
                            for item in person.authored
                        ):
                            person.authored.append(authorship)

        self._record_title_reviews(publications, affected)
        self.prepared.write_models("publications", publications.values())
        self.prepared.write_models("persons", persons.values())
        return {"publications": len(publications), "persons": len(persons)}

    def _record_title_reviews(self, publications: OrderedDict[str, Publication],
                              affected: set[str]) -> None:
        by_title: dict[str, list[Publication]] = {}
        for publication in publications.values():
            if publication.doi:
                continue
            title = " ".join(publication.title.casefold().split())
            by_title.setdefault(title, []).append(publication)
        author_ids = {
            publication.id: {
                author.person_id
                for record in publication.source_records
                for author in record.authors
            }
            for publication in publications.values()
        }
        for same_title in by_title.values():
            for index, first in enumerate(same_title):
                for second in same_title[index + 1:]:
                    if not ({first.id, second.id} & affected):
                        continue
                    shared = sorted(author_ids[first.id] & author_ids[second.id])
                    if shared:
                        record_publication_pair(
                            self.prepared.db, first.id, second.id,
                            title=first.title, shared_authors=shared,
                        )
