from __future__ import annotations

from datetime import date
from uuid import uuid4

from pymongo.errors import DuplicateKeyError

from pauk.models import ExternalIdentifier, Person, Publication
from pauk.models.identity import normalize_external_value

from .contracts import IncomingAuthor, IncomingPublication


class IdentityConflict(RuntimeError):
    """Two exact identifiers point at different canonical entities."""


class IdentityStore:
    COLLECTION = "identity_keys"

    def __init__(self, db) -> None:
        self.collection = db[self.COLLECTION]

    @staticmethod
    def document_id(entity: str, normalized_key: str) -> str:
        return f"{entity}:{normalized_key}"

    def lookup(self, entity: str, normalized_key: str) -> str | None:
        row = self.collection.find_one(
            {"_id": self.document_id(entity, normalized_key)}, {"entity_id": 1}
        )
        return row.get("entity_id") if row else None

    def claim(self, entity: str, identifier: ExternalIdentifier, entity_id: str) -> str:
        key = self.document_id(entity, identifier.normalized_key)
        try:
            self.collection.insert_one({
                "_id": key,
                "entity": entity,
                "scheme": identifier.scheme,
                "normalized_value": identifier.value,
                "entity_id": entity_id,
            })
            return entity_id
        except DuplicateKeyError:
            owner = self.lookup(entity, identifier.normalized_key)
            if owner is None:
                raise
            return owner

    def reassign_if_owner(
        self,
        entity: str,
        identifier: ExternalIdentifier,
        old_owner: str,
        new_owner: str,
    ) -> bool:
        """Repair a provisional claim only while it still has our UUID owner."""
        result = self.collection.update_one(
            {
                "_id": self.document_id(entity, identifier.normalized_key),
                "entity_id": old_owner,
            },
            {"$set": {"entity_id": new_owner}},
        )
        return result.matched_count == 1


class IdentityResolver:
    def __init__(self, db) -> None:
        self.db = db
        self.keys = IdentityStore(db)

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}_{uuid4()}"

    def _publication_by_source(self, incoming: IncomingPublication) -> str | None:
        row = self.db.publications.find_one(
            {"$or": [
                {"source_keys": incoming.record_key},
                {"identifiers.normalized_key": incoming.record_key},
                {"id": incoming.source_id} if incoming.source == "openalex" else {"_id": None},
                {"merged_ids": incoming.source_id} if incoming.source == "openalex" else {"_id": None},
            ]},
            {"id": 1},
        )
        return row.get("id") if row else None

    def _publication_by_doi(self, doi: str | None) -> str | None:
        if not doi:
            return None
        normalized = normalize_external_value("doi", doi)
        rows = list(self.db.publications.find(
            {"identifiers.normalized_key": f"doi:{normalized}"},
            {"id": 1, "merged_ids": 1, "type": 1, "publication_date": 1,
             "year": 1, "title": 1, "journal": 1, "abstract": 1, "pdf_urls": 1},
        ))
        # Compatibility scan for rows that predate identifiers. This is used
        # only until migration backfills the indexed identity collection.
        known = {row.get("id") for row in rows}
        for legacy in self.db.publications.find(
            {"doi": {"$ne": None}},
            {"id": 1, "doi": 1, "merged_ids": 1, "type": 1,
             "publication_date": 1, "year": 1, "title": 1, "journal": 1,
             "abstract": 1, "pdf_urls": 1},
        ):
            if (
                normalize_external_value("doi", legacy.get("doi") or "") == normalized
                and legacy.get("id") not in known
            ):
                rows.append(legacy)
                known.add(legacy.get("id"))
        if not rows:
            return None
        ids = {row.get("id") for row in rows}

        def rank(row: dict) -> tuple:
            publication_type = str(row.get("type") or "").casefold()
            type_rank = 0 if publication_type == "article" else 2 if publication_type == "preprint" else 1
            try:
                published = date.fromisoformat(str(row.get("publication_date"))).toordinal()
            except ValueError:
                published = 0
            completeness = -sum(bool(row.get(field)) for field in (
                "title", "journal", "publication_date", "abstract", "pdf_urls",
            ))
            return (
                0 if (ids - {row.get("id")}).intersection(row.get("merged_ids") or []) else 1,
                type_rank,
                -published,
                -(row.get("year") or 0),
                completeness,
                str(row.get("id")),
            )

        return min(rows, key=rank).get("id")

    def publication_id(self, incoming: IncomingPublication, *, legacy: bool = False) -> str:
        source_identifier = ExternalIdentifier(
            scheme=incoming.source,
            value=incoming.source_id,
            normalized_key=incoming.record_key,
            url=incoming.source_url,
        )
        source_owner = self.keys.lookup("publication", incoming.record_key)
        source_owner = source_owner or self._publication_by_source(incoming)
        doi_identifier = ExternalIdentifier(scheme="doi", value=incoming.doi) if incoming.doi else None
        doi_owner = self.keys.lookup("publication", doi_identifier.normalized_key) if doi_identifier else None
        doi_owner = doi_owner or self._publication_by_doi(incoming.doi)
        if source_owner and doi_owner and source_owner != doi_owner:
            raise IdentityConflict(
                f"{incoming.record_key} belongs to {source_owner}, but DOI belongs to {doi_owner}"
            )
        candidate_id = source_owner or doi_owner or (
            incoming.source_id if legacy and incoming.source == "openalex" else self._new_id("pub")
        )

        # When the source key is already known it is authoritative. Otherwise
        # claim the DOI first, so concurrent ingestion of two different source
        # records with the same DOI converges before either source key is
        # attached to a newly generated UUID.
        if source_owner:
            claimed_source = self.keys.claim("publication", source_identifier, source_owner)
            if claimed_source != source_owner:
                raise IdentityConflict(f"{incoming.record_key} is already owned by {claimed_source}")
            entity_id = source_owner
        elif doi_identifier:
            entity_id = self.keys.claim("publication", doi_identifier, candidate_id)
            claimed_source = self.keys.claim("publication", source_identifier, entity_id)
            if claimed_source != entity_id:
                # A concurrent normalization of this exact source record won
                # after our provisional DOI claim. The UUID is not persisted,
                # so conditionally transfer only that provisional claim.
                if entity_id != candidate_id or doi_owner or not self.keys.reassign_if_owner(
                    "publication", doi_identifier, entity_id, claimed_source
                ):
                    raise IdentityConflict(
                        f"{incoming.record_key} is already owned by {claimed_source}"
                    )
                entity_id = claimed_source
        else:
            entity_id = self.keys.claim("publication", source_identifier, candidate_id)

        if doi_identifier:
            claimed_doi = self.keys.claim("publication", doi_identifier, entity_id)
            if claimed_doi != entity_id:
                raise IdentityConflict(
                    f"DOI {doi_identifier.value} is already owned by {claimed_doi}"
                )
        return entity_id

    def _person_by_identifier(self, identifier: ExternalIdentifier) -> str | None:
        query = {"identifiers.normalized_key": identifier.normalized_key}
        if identifier.scheme == "openalex":
            query = {"$or": [query, {"openalex_id": identifier.value}, {"id": identifier.value}]}
        elif identifier.scheme == "orcid":
            query = {"$or": [query, {"orcid": identifier.value}]}
        row = self.db.persons.find_one(query, {"id": 1})
        return row.get("id") if row else None

    def person_id(self, incoming: IncomingAuthor, source: str, record_key: str, *,
                  legacy: bool = False) -> str:
        identifiers = list(incoming.identifiers)
        if incoming.source_author_id and not any(item.scheme == source for item in identifiers):
            identifiers.append(ExternalIdentifier(
                scheme=source,
                value=incoming.source_author_id,
            ))
        if incoming.orcid and not any(item.scheme == "orcid" for item in identifiers):
            identifiers.append(ExternalIdentifier(scheme="orcid", value=incoming.orcid))
        occurrence = ExternalIdentifier(
            scheme="occurrence",
            value=f"{record_key}:{incoming.position}",
        )
        occurrence_owner = (
            self.keys.lookup("person", occurrence.normalized_key)
            or self._person_by_identifier(occurrence)
        )
        if (not identifiers or occurrence_owner) and not any(
            item.normalized_key == occurrence.normalized_key for item in identifiers
        ):
            identifiers.append(occurrence)
        identifiers = sorted(
            {item.normalized_key: item for item in identifiers}.values(),
            key=lambda item: (
                0 if item.scheme == "orcid" else 1 if item.scheme == source else 2,
                item.normalized_key,
            ),
        )
        owners = {
            owner
            for identifier in identifiers
            if (owner := self.keys.lookup("person", identifier.normalized_key)
                or self._person_by_identifier(identifier))
        }
        if len(owners) > 1:
            raise IdentityConflict(f"author identifiers point at different people: {sorted(owners)}")
        provisional = not owners and not legacy
        if owners:
            entity_id = next(iter(owners))
        elif legacy:
            if incoming.source_author_id:
                entity_id = incoming.source_author_id
            elif incoming.orcid:
                entity_id = f"orcid_{incoming.orcid}"
            else:
                # Old raw rows preserve their deterministic compatibility id;
                # v2 rows below deliberately never key people by name.
                from hashlib import sha1

                name = " ".join((incoming.name or "").split())
                entity_id = f"name_{sha1(name.casefold().encode()).hexdigest()[:12]}"
        else:
            entity_id = self._new_id("per")
        # The orchestrator stores the exact identifiers used to resolve this
        # occurrence on both Person and PublicationSourceRecord. Keeping the
        # resolver as the single constructor prevents adapters from having
        # subtly different identity behaviour.
        incoming.identifiers = identifiers
        claimed_identifiers: list[ExternalIdentifier] = []
        for identifier in identifiers:
            owner = self.keys.claim("person", identifier, entity_id)
            if owner != entity_id:
                if not provisional or not all(
                    self.keys.reassign_if_owner("person", claimed, entity_id, owner)
                    for claimed in claimed_identifiers
                ):
                    raise IdentityConflict(
                        f"author identifier {identifier.normalized_key} is already owned by {owner}"
                    )
                entity_id = owner
                provisional = False
            claimed_identifiers.append(identifier)
        return entity_id

    def publication(self, entity_id: str) -> Publication | None:
        row = self.db.publications.find_one({"id": entity_id}, {"_id": False, "groups": False, "_version": False})
        return Publication.model_validate(row) if row else None

    def person(self, entity_id: str) -> Person | None:
        row = self.db.persons.find_one({"id": entity_id}, {"_id": False, "groups": False, "_version": False})
        return Person.model_validate(row) if row else None
