from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from pathlib import Path

from pauk.ingest.aggregate import build_publication
from pauk.ingest.identity import IdentityStore
from pauk.models import ExternalIdentifier, Person, Publication, PublicationSourceRecord
from pauk.models.relations import Authorship
from pauk.pipeline.normalize import _merge_person
from pauk.pipeline.stages.dedup import merge_rank
from pauk.storage.person_merge_sources import preserve as preserve_person_sources


class MigrationBlocked(RuntimeError):
    pass


class SourceNeutralMigration:
    """Idempotent MongoDB 4.4-safe migration to the v2 identity model.

    Each phase consists only of single-document idempotent writes and records
    completion in `migration_state`. A stopped run can therefore be resumed
    without a multi-document transaction.
    """

    NAME = "source_neutral_v2"

    def __init__(
        self,
        db,
        pdf_dir: Path | None = None,
        batch_size: int = 500,
        *,
        person_aliases: dict[str, str] | None = None,
        graph_publications: list[dict] | None = None,
        graph_persons: list[dict] | None = None,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self.db = db
        self.pdf_dir = pdf_dir
        self.batch_size = batch_size
        self.identities = IdentityStore(db)
        self.person_aliases = dict(person_aliases or {})
        self.person_alias_counts = Counter(self.person_aliases.values())
        self.graph_publications = list(graph_publications or [])
        self.graph_persons = list(graph_persons or [])

    def _publication_rows(self) -> list[dict]:
        cursor = self.db.publications.find({})
        if hasattr(cursor, "batch_size"):
            cursor = cursor.batch_size(self.batch_size)
        return list(cursor)

    def _person_rows(self) -> list[dict]:
        cursor = self.db.persons.find({})
        if hasattr(cursor, "batch_size"):
            cursor = cursor.batch_size(self.batch_size)
        return list(cursor)

    @staticmethod
    def _record_for_legacy(row: dict) -> PublicationSourceRecord | None:
        publication_id = row.get("id") or row.get("_id")
        if not str(publication_id).startswith("W"):
            return None
        return PublicationSourceRecord(
            key=f"openalex:{publication_id}",
            source="openalex",
            source_id=publication_id,
            source_url=row.get("openalex_url"),
            title=row.get("title"),
            type=row.get("type"),
            fields=row.get("fields") or [],
            journal=row.get("journal"),
            doi=row.get("doi"),
            publication_date=row.get("publication_date"),
            year=row.get("year"),
            pdf_urls=row.get("pdf_urls") or [],
            abstract=row.get("abstract"),
            metadata={
                "funding": row.get("funding") or [],
            },
        )

    def _publication_model(self, row: dict) -> Publication:
        clean = {k: v for k, v in row.items() if k not in {"_id", "groups", "_version"}}
        publication = Publication.model_validate(clean)
        if not publication.source_records:
            legacy = self._record_for_legacy(row)
            if legacy:
                publication.source_records = [legacy]
                publication.sources = [legacy.source]
                publication.source_keys = [legacy.key]
                publication.identifiers.append(ExternalIdentifier(
                    scheme=legacy.source, value=legacy.source_id,
                    normalized_key=legacy.key, url=legacy.source_url,
                ))
        if publication.doi:
            doi = ExternalIdentifier(scheme="doi", value=publication.doi)
            if doi.normalized_key not in {item.normalized_key for item in publication.identifiers}:
                publication.identifiers.append(doi)
        if publication.source_records and publication.funding:
            metadata = publication.source_records[0].metadata
            metadata.setdefault(
                "funding",
                [item.model_dump(mode="json") for item in publication.funding],
            )
        publication.schema_version = 2
        return publication

    def _publication_dois(self, row: dict) -> set[str]:
        publication = self._publication_model(row)
        return {
            identifier.value
            for identifier in publication.identifiers
            if identifier.scheme == "doi"
        }

    def _person_model(self, row: dict) -> Person:
        clean = {k: v for k, v in row.items() if k not in {"_id", "groups", "_version"}}
        person = Person.model_validate(clean)
        inferred_openalex = person.openalex_id
        if not inferred_openalex and person.id.startswith("A"):
            inferred_openalex = person.id
        known = {identifier.normalized_key for identifier in person.identifiers}
        for scheme, value in (("openalex", inferred_openalex), ("orcid", person.orcid)):
            if not value:
                continue
            identifier = ExternalIdentifier(scheme=scheme, value=value)
            if identifier.normalized_key not in known:
                person.identifiers.append(identifier)
                known.add(identifier.normalized_key)
        person.schema_version = 2
        return person

    def _person_identity_groups(self, rows: list[dict]) -> list[list[dict]]:
        """Connected people sharing any exact source id or ORCID."""
        by_id = {row.get("id") or row.get("_id"): row for row in rows}
        parent = {row_id: row_id for row_id in by_id}

        def find(row_id: str) -> str:
            while parent[row_id] != row_id:
                parent[row_id] = parent[parent[row_id]]
                row_id = parent[row_id]
            return row_id

        def union(first: str, second: str) -> None:
            first_root, second_root = find(first), find(second)
            if first_root != second_root:
                parent[max(first_root, second_root)] = min(first_root, second_root)

        identifier_owners: dict[str, list[str]] = defaultdict(list)
        for row_id, row in by_id.items():
            for identifier in self._person_model(row).identifiers:
                identifier_owners[identifier.normalized_key].append(row_id)
        for owners in identifier_owners.values():
            for owner in owners[1:]:
                union(owners[0], owner)
        # Graph review/dedup may have made a broader historical fold than
        # exact identifiers alone can reconstruct. Preserve it during the
        # Mongo backfill instead of publishing its aliases under a competing
        # new canonical and thereby either undoing the fold or losing edges.
        # Only exact-identity components need expanding: unrelated graph-only
        # folds remain graph-only, as they were before this migration.
        initial_sizes = Counter(find(row_id) for row_id in by_id)
        identity_component_ids = {
            row_id for row_id in by_id if initial_sizes[find(row_id)] > 1
        }
        for alias, canonical in self.person_aliases.items():
            if alias in identity_component_ids and canonical in by_id:
                union(alias, canonical)
        components: dict[str, list[dict]] = defaultdict(list)
        for row_id, row in by_id.items():
            components[find(row_id)].append(row)
        return [members for members in components.values() if len(members) > 1]

    def _person_canonical(self, members: list[dict]) -> tuple[str | None, list[str]]:
        ids = {row.get("id") or row.get("_id") for row in members}
        identifiers_by_member = [self._person_model(row).identifiers for row in members]
        identifier_counts: dict[str, int] = defaultdict(int)
        for identifiers in identifiers_by_member:
            for normalized_key in {item.normalized_key for item in identifiers}:
                identifier_counts[normalized_key] += 1
        identity_owners = {
            owner
            for identifiers in identifiers_by_member
            for identifier in identifiers
            if identifier_counts[identifier.normalized_key] > 1
            if (owner := self.identities.lookup("person", identifier.normalized_key)) in ids
        }
        if len(identity_owners) > 1:
            return None, sorted(identity_owners)
        graph_owners = {
            self.person_aliases[row_id]
            for row_id in ids
            if self.person_aliases.get(row_id) in ids
        }
        if identity_owners:
            return next(iter(identity_owners)), []
        if graph_owners:
            graph_rows = [
                row for row in members
                if (row.get("id") or row.get("_id")) in graph_owners
            ]
            canonical = min(
                graph_rows,
                key=lambda row: (
                    -self.person_alias_counts[row.get("id") or row.get("_id")],
                    merge_rank(
                        len(row.get("authored") or []),
                        row.get("orcid"),
                        str(row.get("id") or row.get("_id")),
                    ),
                ),
            )
            return canonical.get("id") or canonical.get("_id"), []
        claimed = sorted(
            members,
            key=lambda row: (
                -len(ids.intersection(row.get("merged_ids") or [])),
                merge_rank(
                    len(row.get("authored") or []),
                    row.get("orcid"),
                    str(row.get("id") or row.get("_id")),
                ),
            ),
        )
        return claimed[0].get("id") or claimed[0].get("_id"), []

    def _doi_groups(self, rows: list[dict]) -> dict[str, list[dict]]:
        """Connected publication components sharing any normalized DOI."""
        by_id = {row.get("id") or row.get("_id"): row for row in rows}
        parent = {row_id: row_id for row_id in by_id}

        def find(row_id: str) -> str:
            while parent[row_id] != row_id:
                parent[row_id] = parent[parent[row_id]]
                row_id = parent[row_id]
            return row_id

        def union(first: str, second: str) -> None:
            first_root, second_root = find(first), find(second)
            if first_root != second_root:
                parent[max(first_root, second_root)] = min(first_root, second_root)

        doi_owners: dict[str, list[str]] = defaultdict(list)
        for row in rows:
            row_id = row.get("id") or row.get("_id")
            for doi in self._publication_dois(row):
                doi_owners[doi].append(row_id)
        for owners in doi_owners.values():
            for owner in owners[1:]:
                union(owners[0], owner)
        components: dict[str, list[dict]] = defaultdict(list)
        for row_id, row in by_id.items():
            components[find(row_id)].append(row)
        result = {}
        for members in components.values():
            if len(members) < 2:
                continue
            dois = sorted({doi for row in members for doi in self._publication_dois(row)})
            result[dois[0]] = members
        return result

    def _canonical(self, members: list[dict]) -> str:
        ids = {row.get("id") or row.get("_id") for row in members}
        identity_owners = {
            owner
            for row in members
            for doi in self._publication_dois(row)
            if (owner := self.identities.lookup("publication", f"doi:{doi}")) in ids
        }
        if len(identity_owners) > 1:
            raise MigrationBlocked(
                f"DOI component has multiple assigned owners: {sorted(identity_owners)}"
            )
        if identity_owners:
            return next(iter(identity_owners))
        for row in members:
            row_id = row.get("id") or row.get("_id")
            if any(merged in ids for merged in row.get("merged_ids") or []):
                return row_id

        def rank(row: dict) -> tuple:
            publication_type = str(row.get("type") or "").casefold()
            type_rank = 0 if publication_type == "article" else 2 if publication_type == "preprint" else 1
            publication_date = row.get("publication_date")
            try:
                date_rank = date.fromisoformat(str(publication_date)).toordinal() if publication_date else 0
            except ValueError:
                date_rank = 0
            completeness = -sum(bool(row.get(field)) for field in (
                "title", "journal", "publication_date", "abstract", "pdf_urls",
            ))
            return (
                type_rank,
                -date_rank,
                -(row.get("year") or 0),
                completeness,
                str(row.get("id") or row.get("_id")),
            )

        return min(members, key=rank).get("id") or min(members, key=rank).get("_id")

    def plan(self) -> dict:
        rows = self._publication_rows()
        person_rows = self._person_rows()
        doi_groups = self._doi_groups(rows)
        canonical_by_component = {
            doi: self._canonical(members) for doi, members in doi_groups.items()
        }
        component_owner = {
            row.get("id") or row.get("_id"): canonical_by_component[doi]
            for doi, members in doi_groups.items()
            for row in members
        }
        source_owners: dict[str, set[str]] = defaultdict(set)
        for row in rows:
            publication = self._publication_model(row)
            for key in publication.source_keys:
                source_owners[key].add(publication.id)
        source_collisions = {
            key: sorted(owners) for key, owners in source_owners.items() if len(owners) > 1
        }
        resolvable_source_collisions = {
            key: owners
            for key, owners in source_collisions.items()
            if len({component_owner.get(owner, owner) for owner in owners}) == 1
            and any(component_owner.get(owner) != owner for owner in owners)
        }
        blocking_source_collisions = {
            key: owners
            for key, owners in source_collisions.items()
            if key not in resolvable_source_collisions
        }
        duplicate_ids = {
            row.get("id") or row.get("_id")
            for members in doi_groups.values()
            for row in members
        }
        canonical_ids = set(canonical_by_component.values())
        identity_key_conflicts = []
        for row in rows:
            publication = self._publication_model(row)
            expected = component_owner.get(publication.id, publication.id)
            for identifier in publication.identifiers:
                owner = self.identities.lookup("publication", identifier.normalized_key)
                if owner is not None and owner not in {publication.id, expected}:
                    identity_key_conflicts.append({
                        "key": identifier.normalized_key,
                        "publication": publication.id,
                        "owner": owner,
                        "expected": expected,
                    })
        person_components = self._person_identity_groups(person_rows)
        person_component_plan = []
        person_identity_conflicts = []
        removed_person_ids: set[str] = set()
        for members in person_components:
            canonical, conflicts = self._person_canonical(members)
            member_ids = sorted(row.get("id") or row.get("_id") for row in members)
            person_component_plan.append({"members": member_ids, "canonical": canonical})
            if conflicts:
                person_identity_conflicts.append({
                    "members": member_ids,
                    "assigned_owners": conflicts,
                })
            elif canonical:
                removed_person_ids.update(set(member_ids) - {canonical})
        removed_ids = duplicate_ids - canonical_ids
        override_conflicts = list(self.db.graph_overrides.find(
            {"active": True, "label": "Publication", "target_id": {"$in": list(removed_ids)}},
            {"_id": 1, "target_id": 1},
        ))
        person_override_conflicts = list(self.db.graph_overrides.find(
            {"active": True, "label": "Person", "target_id": {"$in": list(removed_person_ids)}},
            {"_id": 1, "target_id": 1},
        ))
        publication_ids = {
            row.get("id") or row.get("_id") for row in rows
        }
        person_ids = {
            row.get("id") or row.get("_id") for row in person_rows
        }
        graph_person_ids = {
            row.get("properties", {}).get("id") for row in self.graph_persons
        }
        graph_person_ids.discard(None)
        graph_publication_ids = {
            row.get("properties", {}).get("id") for row in self.graph_publications
        }
        graph_publication_ids.discard(None)
        graph_legacy_identity_conflicts = []
        graph_legacy_unresolved_authors = []
        for row in self.graph_persons:
            person = self._legacy_person_model(row)
            for identifier in person.identifiers:
                owner = self.identities.lookup("person", identifier.normalized_key)
                if owner is not None and owner != person.id:
                    graph_legacy_identity_conflicts.append({
                        "entity": "person", "id": person.id,
                        "key": identifier.normalized_key, "owner": owner,
                    })
        for row in self.graph_publications:
            publication = self._legacy_publication_model(row, person_ids | graph_person_ids)
            for identifier in publication.identifiers:
                owner = self.identities.lookup("publication", identifier.normalized_key)
                if owner is not None and owner != publication.id:
                    graph_legacy_identity_conflicts.append({
                        "entity": "publication", "id": publication.id,
                        "key": identifier.normalized_key, "owner": owner,
                    })
            for author in row.get("authors") or []:
                person_id = author.get("person_id")
                canonical = self.person_aliases.get(person_id, person_id)
                if canonical not in person_ids | graph_person_ids:
                    graph_legacy_unresolved_authors.append({
                        "publication": publication.id,
                        "person": person_id,
                    })
        resolvable_source_authors = 0
        dangling_source_authors = 0
        for publication in rows:
            for record in publication.get("source_records") or publication.get("versions") or []:
                for author in record.get("authors") or []:
                    person_id = author.get("person_id")
                    if not person_id or person_id in person_ids:
                        continue
                    canonical = self.person_aliases.get(person_id)
                    if canonical in person_ids:
                        resolvable_source_authors += 1
                    else:
                        dangling_source_authors += 1
        dangling_authorships = self.db.persons.count_documents({
            "authored": {"$elemMatch": {"publication_id": {"$nin": list(publication_ids)}}}
        })
        dangling_repo_links = self.db.repo_links.count_documents({
            "publication_id": {"$nin": list(publication_ids)}
        })
        dangling_repositories = self.db.repositories.count_documents({
            "publication_ids": {"$elemMatch": {"$nin": list(publication_ids)}}
        })
        pdf_without_pointer = 0
        if self.pdf_dir and self.pdf_dir.exists():
            pointed = {row.get("filename") or f"{row['_id']}.pdf" for row in self.db.pdfs.find({})}
            pdf_without_pointer = sum(path.name not in pointed for path in self.pdf_dir.glob("*.pdf"))
        return {
            "publications": len(rows),
            "persons": len(person_rows),
            "source_key_collisions": source_collisions,
            "resolvable_source_key_collisions": resolvable_source_collisions,
            "blocking_source_key_collisions": blocking_source_collisions,
            "identity_key_conflicts": identity_key_conflicts,
            "person_identity_components": person_component_plan,
            "person_identity_conflicts": person_identity_conflicts,
            "doi_components": {
                doi: {
                    "members": sorted(row.get("id") or row.get("_id") for row in members),
                    "canonical": canonical_by_component[doi],
                }
                for doi, members in doi_groups.items()
            },
            "dangling_references": {
                "persons": dangling_authorships,
                "repo_links": dangling_repo_links,
                "repositories": dangling_repositories,
                "source_authors": dangling_source_authors,
            },
            "resolvable_source_author_aliases": resolvable_source_authors,
            "pdf_without_pointer": pdf_without_pointer,
            "override_conflicts": override_conflicts,
            "person_override_conflicts": person_override_conflicts,
            "graph_only_publications": sorted(graph_publication_ids),
            "graph_only_persons": sorted(graph_person_ids),
            "graph_legacy_identity_conflicts": graph_legacy_identity_conflicts,
            "graph_legacy_unresolved_authors": graph_legacy_unresolved_authors,
        }

    def apply(self) -> dict:
        plan = self.plan()
        if plan["blocking_source_key_collisions"]:
            raise MigrationBlocked("source record keys have multiple publication owners")
        if plan["identity_key_conflicts"]:
            raise MigrationBlocked("identity keys disagree with planned canonical owners")
        if plan["person_identity_conflicts"]:
            raise MigrationBlocked("person identity keys have multiple assigned owners")
        if plan["graph_legacy_identity_conflicts"]:
            raise MigrationBlocked("graph-only entities conflict with assigned identity keys")
        if plan["graph_legacy_unresolved_authors"]:
            raise MigrationBlocked("graph-only publications have unresolved authors")
        if plan["override_conflicts"]:
            raise MigrationBlocked("active overrides target DOI duplicates that would be removed")
        if plan["person_override_conflicts"]:
            raise MigrationBlocked("active overrides target person duplicates that would be removed")
        if any(plan["dangling_references"].values()):
            raise MigrationBlocked("dangling publication references must be resolved before apply")
        state = self.db.migration_state.find_one({"_id": self.NAME}) or {}
        completed = set(state.get("completed") or [])
        counts: dict[str, int] = {}
        phases = (
            ("import_graph_legacy_entities", self._import_graph_legacy_entities),
            ("backfill_publications", self._backfill_publications),
            ("backfill_persons", self._backfill_persons),
            ("merge_doi_duplicates", self._merge_doi_duplicates),
            ("merge_person_identity_duplicates", self._merge_person_identity_duplicates),
            ("repoint_graph_person_aliases", self._repoint_graph_person_aliases),
            ("backfill_identity_keys", self._backfill_identity_keys),
            ("backfill_pdf_pointers", self._backfill_pdf_pointers),
        )
        for phase, action in phases:
            if phase in completed:
                counts[phase] = 0
                continue
            counts[phase] = action()
            self.db.migration_state.update_one(
                {"_id": self.NAME},
                {"$addToSet": {"completed": phase}, "$set": {"updated_at": datetime.now(UTC)}},
                upsert=True,
            )
        return counts

    @staticmethod
    def _legacy_person_identifier(person_id: str) -> ExternalIdentifier:
        scheme = "openalex" if person_id.startswith("A") else "manual"
        return ExternalIdentifier(scheme=scheme, value=person_id)

    def _legacy_person_model(self, row: dict) -> Person:
        properties = row.get("properties") or {}
        clean = {
            key: value
            for key, value in properties.items()
            if key in Person.model_fields and key not in {"created_at", "enriched_at"}
        }
        person_id = str(clean["id"])
        clean.update({
            "schema_version": 2,
            "identifiers": [self._legacy_person_identifier(person_id)],
            "openalex_id": person_id if person_id.startswith("A") else clean.get("openalex_id"),
            "is_itmo": bool(clean.get("is_itmo", False)),
            "authored": row.get("authored") or [],
        })
        return Person.model_validate(clean)

    def _legacy_publication_model(self, row: dict, person_ids: set[str]) -> Publication:
        properties = row.get("properties") or {}
        clean = {
            key: value
            for key, value in properties.items()
            if key in Publication.model_fields
        }
        publication_id = str(clean["id"])
        source = "openalex" if publication_id.startswith("W") else "manual"
        authors = []
        for author in row.get("authors") or []:
            person_id = author.get("person_id")
            canonical = self.person_aliases.get(person_id, person_id)
            if canonical not in person_ids:
                continue
            authors.append({**author, "person_id": canonical})
        record = PublicationSourceRecord(
            source=source,
            source_id=publication_id,
            source_url=clean.get("openalex_url"),
            title=clean.get("title"),
            type=clean.get("type"),
            fields=clean.get("fields") or [],
            journal=clean.get("journal"),
            doi=clean.get("doi"),
            publication_date=clean.get("publication_date"),
            year=clean.get("year"),
            pdf_urls=clean.get("pdf_urls") or [],
            abstract=clean.get("abstract"),
            authors=authors,
            metadata={"migrated_from": "neo4j"},
        )
        clean.update({
            "schema_version": 2,
            "source_records": [record],
            "identifiers": [],
            "sources": [],
            "source_keys": [],
            "versions": [],
        })
        return Publication.model_validate(clean)

    def _import_graph_legacy_entities(self) -> int:
        changed = 0
        for row in self.graph_persons:
            person = self._legacy_person_model(row)
            if self.db.persons.find_one({"id": person.id}, {"_id": 1}):
                continue
            document = person.model_dump(mode="json", by_alias=True)
            document.update({"_id": person.id, "groups": ["legacy-graph"]})
            self.db.persons.insert_one(document)
            for identifier in person.identifiers:
                owner = self.identities.claim("person", identifier, person.id)
                if owner != person.id:
                    raise MigrationBlocked(f"{identifier.normalized_key} is owned by {owner}")
            changed += 1

        person_ids = {
            row.get("id") or row.get("_id")
            for row in self.db.persons.find({}, {"id": 1})
        }
        for row in self.graph_publications:
            publication = self._legacy_publication_model(row, person_ids)
            if self.db.publications.find_one({"id": publication.id}, {"_id": 1}):
                continue
            document = publication.model_dump(mode="json", by_alias=True)
            document.update({"_id": publication.id, "groups": ["legacy-graph"]})
            self.db.publications.insert_one(document)
            for identifier in publication.identifiers:
                owner = self.identities.claim("publication", identifier, publication.id)
                if owner != publication.id:
                    raise MigrationBlocked(f"{identifier.normalized_key} is owned by {owner}")
            for author in publication.source_records[0].authors:
                self._add_legacy_authorship(author.person_id, publication.id, author)
            changed += 1
        return changed

    def _add_legacy_authorship(self, person_id: str, publication_id: str, author) -> None:
        row = self.db.persons.find_one({"id": person_id})
        if row is None:
            return
        person = self._person_model(row)
        key = publication_id, author.position
        if key not in {(item.publication_id, item.position) for item in person.authored}:
            person.authored.append(Authorship(
                publication_id=publication_id,
                position=author.position,
                affiliation=author.affiliation,
                is_corresponding=author.is_corresponding,
            ))
        groups = list(dict.fromkeys([*(row.get("groups") or []), "legacy-graph"]))
        self.db.persons.update_one(
            {"_id": row["_id"]},
            {"$set": {
                "authored": [item.model_dump(mode="json") for item in person.authored],
                "groups": groups,
            }},
        )

    def _backfill_publications(self) -> int:
        changed = 0
        for row in self._publication_rows():
            publication = self._publication_model(row)
            if publication.source_records:
                publication = build_publication(
                    publication.id,
                    publication.source_records,
                    publication,
                    reset_enrichment=False,
                )
            update = {
                "schema_version": 2,
                "identifiers": [item.model_dump(mode="json") for item in publication.identifiers],
                "source_records": [item.model_dump(mode="json") for item in publication.source_records],
                "sources": publication.sources,
                "source_keys": publication.source_keys,
                # Dual write for the compatibility release.
                "versions": [item.model_dump(mode="json") for item in publication.versions],
                "openalex_url": publication.openalex_url,
            }
            result = self.db.publications.update_one({"_id": row["_id"]}, {"$set": update})
            changed += result.modified_count
        return changed

    def _backfill_persons(self) -> int:
        changed = 0
        for row in self._person_rows():
            person = self._person_model(row)
            result = self.db.persons.update_one(
                {"_id": row["_id"]},
                {"$set": {
                    "schema_version": 2,
                    "identifiers": [
                        identifier.model_dump(mode="json") for identifier in person.identifiers
                    ],
                }},
            )
            changed += result.modified_count
        return changed

    def _merge_person_identity_duplicates(self) -> int:
        merged = 0
        for members in self._person_identity_groups(self._person_rows()):
            canonical_id, conflicts = self._person_canonical(members)
            if conflicts or canonical_id is None:
                raise MigrationBlocked("person identity component has multiple assigned owners")
            duplicate_ids = [
                row.get("id") or row.get("_id")
                for row in members
                if (row.get("id") or row.get("_id")) != canonical_id
            ]
            preserve_person_sources(self.db, [canonical_id, *duplicate_ids])
            canonical_row = self.db.persons.find_one({"id": canonical_id})
            if canonical_row is None:
                continue
            canonical = self._person_model(canonical_row)
            groups = list(canonical_row.get("groups") or [])
            for duplicate_id in duplicate_ids:
                duplicate_row = self.db.persons.find_one({"id": duplicate_id})
                if duplicate_row is None:
                    continue
                duplicate = self._person_model(duplicate_row)
                _merge_person(canonical, duplicate)
                canonical.merged_ids = list(dict.fromkeys([
                    *canonical.merged_ids,
                    *duplicate.merged_ids,
                    duplicate_id,
                ]))
                groups = list(dict.fromkeys([*groups, *(duplicate_row.get("groups") or [])]))
                self._repoint_person_references(duplicate_id, canonical_id)
                self.db.persons.delete_one({"_id": duplicate_row["_id"]})
                merged += 1
            canonical.identifiers = list({
                identifier.normalized_key: identifier
                for identifier in canonical.identifiers
            }.values())
            canonical.authored = self._merge_authorships(canonical.authored)
            update = canonical.model_dump(mode="json", by_alias=True)
            update["groups"] = groups
            self.db.persons.update_one({"_id": canonical_row["_id"]}, {"$set": update})
        return merged

    def _repoint_graph_person_aliases(self) -> int:
        """Canonicalize legacy source-author ids folded only in Neo4j."""
        person_ids = {
            row.get("id") or row.get("_id")
            for row in self.db.persons.find({}, {"id": 1})
        }
        aliases = {
            alias: canonical
            for alias, canonical in self.person_aliases.items()
            if alias != canonical and canonical in person_ids
        }
        if not aliases:
            return 0
        changed = 0
        for publication in self.db.publications.find({}, {"source_records": 1, "versions": 1}):
            update = {}
            for field in ("source_records", "versions"):
                records = publication.get(field) or []
                field_changed = False
                for record in records:
                    for author in record.get("authors") or []:
                        canonical = aliases.get(author.get("person_id"))
                        if canonical:
                            author["person_id"] = canonical
                            field_changed = True
                if field_changed:
                    update[field] = records
            if update:
                changed += self.db.publications.update_one(
                    {"_id": publication["_id"]}, {"$set": update},
                ).modified_count
        return changed

    @staticmethod
    def _merge_authorships(authorships: list) -> list:
        by_key = {}
        for authorship in authorships:
            key = authorship.publication_id, authorship.position
            current = by_key.get(key)
            if current is None:
                by_key[key] = authorship
                continue
            if current.affiliation is None:
                current.affiliation = authorship.affiliation
            if current.affiliation_source is None:
                current.affiliation_source = authorship.affiliation_source
            current.is_corresponding = current.is_corresponding or authorship.is_corresponding
        return list(by_key.values())

    def _repoint_person_references(self, duplicate_id: str, canonical_id: str) -> None:
        self.db.identity_keys.update_many(
            {"entity": "person", "entity_id": duplicate_id},
            {"$set": {"entity_id": canonical_id}},
        )
        for publication in self.db.publications.find({
            "$or": [
                {"source_records.authors.person_id": duplicate_id},
                {"versions.authors.person_id": duplicate_id},
            ]
        }):
            changed = False
            for field in ("source_records", "versions"):
                for record in publication.get(field) or []:
                    for author in record.get("authors") or []:
                        if author.get("person_id") == duplicate_id:
                            author["person_id"] = canonical_id
                            changed = True
            if changed:
                self.db.publications.update_one(
                    {"_id": publication["_id"]},
                    {"$set": {
                        "source_records": publication.get("source_records") or [],
                        "versions": publication.get("versions") or [],
                    }},
                )

    def _merge_doi_duplicates(self) -> int:
        merged = 0
        for members in self._doi_groups(self._publication_rows()).values():
            canonical_id = self._canonical(members)
            duplicate_ids = [
                row.get("id") or row.get("_id") for row in members
                if (row.get("id") or row.get("_id")) != canonical_id
            ]
            canonical = self.db.publications.find_one({"id": canonical_id})
            if not canonical:
                continue
            groups = list(canonical.get("groups") or [])
            merged_ids = list(canonical.get("merged_ids") or [])
            source_records = list(canonical.get("source_records") or [])
            identifiers = list(canonical.get("identifiers") or [])
            for duplicate_id in duplicate_ids:
                duplicate = self.db.publications.find_one({"id": duplicate_id})
                if not duplicate:
                    continue
                groups = list(dict.fromkeys([*groups, *(duplicate.get("groups") or [])]))
                merged_ids = list(dict.fromkeys([
                    *merged_ids, *(duplicate.get("merged_ids") or []), duplicate_id,
                ]))
                by_key = {item["key"]: item for item in source_records}
                for item in duplicate.get("source_records") or []:
                    current = by_key.get(item["key"])
                    if current is None or self._source_record_score(item) > self._source_record_score(current):
                        by_key[item["key"]] = item
                source_records = list(by_key.values())
                by_identifier = {item["normalized_key"]: item for item in identifiers}
                for item in duplicate.get("identifiers") or []:
                    by_identifier.setdefault(item["normalized_key"], item)
                identifiers = list(by_identifier.values())
                self._rewrite_references(duplicate_id, canonical_id)
                self.db.publications.delete_one({"_id": duplicate["_id"]})
                merged += 1
            combined = dict(canonical)
            combined.update({
                "groups": groups,
                "merged_ids": merged_ids,
                "source_records": source_records,
                "source_keys": list(dict.fromkeys(item["key"] for item in source_records)),
                "sources": list(dict.fromkeys(item["source"] for item in source_records)),
                "identifiers": identifiers,
            })
            publication = self._publication_model(combined)
            if publication.source_records:
                publication = build_publication(
                    canonical_id,
                    publication.source_records,
                    publication,
                    reset_enrichment=False,
                )
            update = publication.model_dump(mode="json", by_alias=True)
            update["groups"] = groups
            self.db.publications.update_one({"_id": canonical["_id"]}, {"$set": update})
        return merged

    @staticmethod
    def _source_record_score(item: dict) -> int:
        """Prefer the richer copy when a historical merge duplicated a source key."""
        record = PublicationSourceRecord.model_validate(item)
        return sum(bool(value) for value in (
            record.source_url,
            record.title,
            record.type,
            record.fields,
            record.journal,
            record.doi,
            record.publication_date,
            record.year,
            record.pdf_urls,
            record.abstract,
            record.authors,
            record.metadata,
        ))

    def _rewrite_references(self, duplicate_id: str, canonical_id: str) -> None:
        # Source keys of a legacy duplicate are reassigned as part of the
        # one-time canonicalisation. Once migration completes, normal ingest
        # never changes an identity-key owner.
        self.db.identity_keys.update_many(
            {"entity": "publication", "entity_id": duplicate_id},
            {"$set": {"entity_id": canonical_id}},
        )
        for person in self.db.persons.find({"authored.publication_id": duplicate_id}):
            authored = person.get("authored") or []
            for item in authored:
                if item.get("publication_id") == duplicate_id:
                    item["publication_id"] = canonical_id
            unique = {(item.get("publication_id"), item.get("position")): item for item in authored}
            self.db.persons.update_one({"_id": person["_id"]}, {"$set": {"authored": list(unique.values())}})
        duplicate_link = self.db.repo_links.find_one({"publication_id": duplicate_id})
        canonical_link = self.db.repo_links.find_one({"publication_id": canonical_id})
        if duplicate_link:
            if canonical_link:
                links = list(canonical_link.get("links") or [])
                for link in duplicate_link.get("links") or []:
                    if link not in links:
                        links.append(link)
                groups = list(dict.fromkeys([
                    *(canonical_link.get("groups") or []), *(duplicate_link.get("groups") or []),
                ]))
                self.db.repo_links.update_one({"_id": canonical_link["_id"]}, {"$set": {
                    "links": links, "groups": groups,
                }})
                self.db.repo_links.delete_one({"_id": duplicate_link["_id"]})
            else:
                replacement = dict(duplicate_link)
                replacement["_id"] = canonical_id
                replacement["publication_id"] = canonical_id
                self.db.repo_links.replace_one({"_id": canonical_id}, replacement, upsert=True)
                self.db.repo_links.delete_one({"_id": duplicate_link["_id"]})
        for repository in self.db.repositories.find({"publication_ids": duplicate_id}):
            publication_ids = list(dict.fromkeys(
                canonical_id if value == duplicate_id else value
                for value in repository.get("publication_ids") or []
            ))
            self.db.repositories.update_one(
                {"_id": repository["_id"]}, {"$set": {"publication_ids": publication_ids}}
            )

    def _backfill_identity_keys(self) -> int:
        inserted = 0
        publications = self.db.publications.find({})
        if hasattr(publications, "batch_size"):
            publications = publications.batch_size(self.batch_size)
        for row in publications:
            for item in row.get("identifiers") or []:
                identifier = ExternalIdentifier.model_validate(item)
                before = self.identities.lookup("publication", identifier.normalized_key)
                owner = self.identities.claim("publication", identifier, row["id"])
                if owner != row["id"]:
                    raise MigrationBlocked(f"{identifier.normalized_key} is owned by {owner}")
                inserted += before is None
        persons = self.db.persons.find({})
        if hasattr(persons, "batch_size"):
            persons = persons.batch_size(self.batch_size)
        for row in persons:
            for item in row.get("identifiers") or []:
                identifier = ExternalIdentifier.model_validate(item)
                before = self.identities.lookup("person", identifier.normalized_key)
                owner = self.identities.claim("person", identifier, row["id"])
                if owner != row["id"]:
                    raise MigrationBlocked(f"{identifier.normalized_key} is owned by {owner}")
                inserted += before is None
        return inserted

    def _backfill_pdf_pointers(self) -> int:
        changed = 0
        pointers = self.db.pdfs.find({})
        if hasattr(pointers, "batch_size"):
            pointers = pointers.batch_size(self.batch_size)
        for row in list(pointers):
            old_key = str(row["_id"])
            filename = row.get("filename") or f"{old_key}.pdf"
            result = self.db.pdfs.update_one({"_id": row["_id"]}, {"$set": {
                "record_key": old_key,
                "filename": filename,
            }})
            changed += result.modified_count
            if old_key.startswith("W"):
                record_key = f"openalex:{old_key}"
                self.db.pdfs.update_one(
                    {"_id": record_key},
                    {"$setOnInsert": {
                        "record_key": record_key,
                        "filename": filename,
                        "fetched_at": row.get("fetched_at"),
                    }},
                    upsert=True,
                )
        if self.pdf_dir and self.pdf_dir.exists():
            pointed = {row.get("filename") for row in self.db.pdfs.find({}, {"filename": 1})}
            for path in self.pdf_dir.glob("W*.pdf"):
                if path.name in pointed:
                    continue
                legacy_id = path.stem
                fetched_at = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
                for record_key in (legacy_id, f"openalex:{legacy_id}"):
                    result = self.db.pdfs.update_one(
                        {"_id": record_key},
                        {"$setOnInsert": {
                            "record_key": record_key,
                            "filename": path.name,
                            "fetched_at": fetched_at,
                        }},
                        upsert=True,
                    )
                    changed += bool(result.upserted_id)
        return changed
