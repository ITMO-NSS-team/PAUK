import tempfile
import unittest
from pathlib import Path

import mongomock

from pauk.graph.extract import NODE_REGISTRY, extract_node
from pauk.ingest import (
    AdapterRegistry,
    IdentityResolver,
    IncomingAuthor,
    IncomingPublication,
    PublicationAdapter,
    PublicationNormalizer,
    RawEnvelope,
)
from pauk.models import Person, Publication
from pauk.models.processing import ProcessingState, ProcessingStatus
from pauk.pipeline.stages.dedup import DedupStage
from pauk.settings import Settings
from pauk.storage import PreparedStore, RawStore, review


class FakeAdapter(PublicationAdapter):
    def __init__(self, source: str):
        self.source = source

    def adapt(self, envelope: RawEnvelope) -> IncomingPublication:
        payload = envelope.payload
        return IncomingPublication(
            source=self.source,
            source_id=payload["source_id"],
            record_key=envelope.record_key,
            source_url=payload.get("source_url"),
            title=payload["title"],
            type=payload.get("type"),
            doi=payload.get("doi"),
            authors=[
                IncomingAuthor(
                    source_author_id=author.get("source_author_id"),
                    name=author.get("name"),
                    orcid=author.get("orcid"),
                    position=position,
                )
                for position, author in enumerate(payload.get("authors") or [], start=1)
            ],
        )


class SourceNeutralIngestTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = Settings(data_dir=Path(temporary.name))
        self.db = mongomock.MongoClient().db
        self.raw = RawStore(self.db, "sample")
        self.prepared = PreparedStore(self.db, "sample")
        self.registry = AdapterRegistry([FakeAdapter("catalog"), FakeAdapter("archive")])

    def append(self, source: str, source_id: str, **payload):
        self.raw.append(
            f"{source}_publications",
            {"source_id": source_id, **payload},
            {},
            provider=source,
            record_key=f"{source}:{source_id}",
            schema_version=2,
        )

    def normalize(self, source="catalog"):
        return PublicationNormalizer(
            self.raw, self.prepared, self.registry,
        ).run(source)

    def test_fake_source_has_no_openalex_fields_and_gets_pauk_ids(self):
        self.append("catalog", "paper/α:1", title="Workshop paper", authors=[{"name": "Ada"}])
        self.normalize()
        publication = next(self.prepared.read_models("publications", Publication))
        person = next(self.prepared.read_models("persons", Person))
        self.assertTrue(publication.id.startswith("pub_"))
        self.assertTrue(person.id.startswith("per_"))
        self.assertEqual(publication.source_keys, ["catalog:paper/α:1"])
        self.assertIsNone(person.openalex_id)

    def test_fake_source_contract_reaches_enrichment_and_graph_mapping(self):
        self.append(
            "catalog", "P1", title="Workshop paper",
            authors=[{"name": "Ada", "source_author_id": "author-1"}],
        )
        self.normalize()
        DedupStage(self.prepared, self.raw, self.config).run()
        publication = next(self.prepared.read_models("publications", Publication))
        row = publication.model_dump(mode="json", by_alias=True)
        label, (node_id, props) = extract_node(row, NODE_REGISTRY["publication"])
        self.assertEqual(label, "Publication")
        self.assertTrue(node_id.startswith("pub_"))
        self.assertIn("catalog:P1", props["source_records"])

    def test_legacy_versions_follow_authoritative_source_record_authors(self):
        publication = Publication(
            id="pub_1",
            title="Paper",
            schema_version=2,
            source_records=[{
                "source": "openalex",
                "source_id": "W1",
                "title": "Paper",
                "authors": [{"person_id": "per_1", "name": "Ada", "position": 1}],
            }],
            versions=[{"openalex_id": "W1", "authors": []}],
        )
        self.assertEqual(
            [author.person_id for author in publication.versions[0].authors],
            ["per_1"],
        )

    def test_dedup_does_not_replace_resolved_version_authors_with_raw_ids(self):
        self.raw.append(
            "openalex_works",
            {
                "id": "https://openalex.org/W1",
                "title": "Paper",
                "authorships": [{"author": {"id": "https://openalex.org/A1"}}],
            },
            {},
        )
        self.prepared.write_models("publications", [Publication(
            id="pub_1",
            title="Paper",
            schema_version=2,
            source_records=[{
                "source": "openalex",
                "source_id": "W1",
                "title": "Paper",
                "authors": [{"person_id": "per_1", "name": "Ada", "position": 1}],
            }],
        )])
        self.prepared.write_models("persons", [Person(
            id="per_1",
            schema_version=2,
            identifiers=[{"scheme": "openalex", "value": "A1"}],
            openalex_id="A1",
            is_itmo=False,
            name_raw="Ada",
        )])
        DedupStage(self.prepared, self.raw, self.config).run()
        publication = next(self.prepared.read_models("publications", Publication))
        self.assertEqual(
            [author.person_id for author in publication.versions[0].authors],
            ["per_1"],
        )

    def test_same_source_record_is_idempotent_and_keeps_processing(self):
        self.append("catalog", "P1", title="Paper")
        self.normalize()
        publication = next(self.prepared.read_models("publications", Publication))
        publication.processing["code_links"] = ProcessingState(status=ProcessingStatus.COMPLETED)
        self.prepared.write_models("publications", [publication])
        version = self.db.publications.find_one({"id": publication.id})["_version"]
        self.normalize()
        after = self.db.publications.find_one({"id": publication.id})
        self.assertEqual(after["_version"], version)
        self.assertEqual(after["_processing"]["code_links"]["status"], "completed")

    def test_rerunning_one_of_two_sources_compares_records_without_order(self):
        self.append(
            "catalog", "P1", title="Article", type="article", doi="10.1/same",
        )
        self.normalize("catalog")
        self.append(
            "archive", "A1", title="Preprint", type="preprint", doi="10.1/same",
        )
        self.normalize("archive")
        publication = next(self.prepared.read_models("publications", Publication))
        publication.full_text = "already parsed"
        publication.processing["code_links"] = ProcessingState(
            status=ProcessingStatus.COMPLETED,
        )
        self.prepared.write_models("publications", [publication])

        self.normalize("catalog")

        unchanged = next(self.prepared.read_models("publications", Publication))
        self.assertEqual(unchanged.full_text, "already parsed")
        self.assertEqual(
            unchanged.processing["code_links"].status,
            ProcessingStatus.COMPLETED,
        )

    def test_changed_source_record_resets_dependent_enrichment(self):
        self.append("catalog", "P1", title="Old title")
        self.normalize()
        publication = next(self.prepared.read_models("publications", Publication))
        publication.has_code = True
        publication.processing["code_links"] = ProcessingState(status=ProcessingStatus.COMPLETED)
        self.prepared.write_models("publications", [publication])
        self.append("catalog", "P1", title="New title")
        self.normalize()
        changed = next(self.prepared.read_models("publications", Publication))
        self.assertEqual(changed.title, "New title")
        self.assertFalse(changed.has_code)
        self.assertEqual(changed.processing, {})

    def test_v2_openalex_record_keeps_w_id_external(self):
        raw = RawStore(self.db, "openalex-sample")
        prepared = PreparedStore(self.db, "openalex-sample")
        raw.append(
            "openalex_works",
            {"id": "https://openalex.org/W99", "title": "Paper", "authorships": []},
            {},
            provider="openalex",
            record_key="openalex:W99",
            schema_version=2,
        )
        PublicationNormalizer(raw, prepared).run("openalex")
        publication = next(prepared.read_models("publications", Publication))
        self.assertTrue(publication.id.startswith("pub_"))
        self.assertIn("openalex:W99", {
            identifier.normalized_key for identifier in publication.identifiers
        })

    def test_rerunning_v2_openalex_keeps_one_pauk_person(self):
        raw = RawStore(self.db, "openalex-sample")
        prepared = PreparedStore(self.db, "openalex-sample")
        raw.append(
            "openalex_works",
            {
                "id": "https://openalex.org/W99",
                "title": "Paper",
                "authorships": [{
                    "author": {
                        "id": "https://openalex.org/A99",
                        "display_name": "Ada",
                    },
                }],
            },
            {},
            provider="openalex",
            record_key="openalex:W99",
            schema_version=2,
        )

        PublicationNormalizer(raw, prepared).run("openalex")
        PublicationNormalizer(raw, prepared).run("openalex")

        [person] = list(prepared.read_models("persons", Person))
        self.assertTrue(person.id.startswith("per_"))
        self.assertEqual(person.openalex_id, "A99")

    def test_new_doi_source_preserves_unmigrated_legacy_authors(self):
        self.prepared.write_models("publications", [Publication(
            id="W1",
            title="Legacy article",
            type="article",
            doi="10.1/same",
        )])
        self.prepared.write_models("persons", [Person(
            id="A1",
            openalex_id="A1",
            is_itmo=False,
            name_raw="Legacy Author",
            authored=[{"publication_id": "W1", "position": 1}],
        )])
        self.append(
            "archive",
            "A1",
            title="Archive copy",
            type="preprint",
            doi="10.1/same",
            authors=[],
        )

        self.normalize("archive")

        publication = next(self.prepared.read_models("publications", Publication))
        legacy_record = next(
            record for record in publication.source_records
            if record.key == "openalex:W1"
        )
        self.assertEqual(publication.title, "Legacy article")
        self.assertEqual(
            [author.person_id for author in legacy_record.authors],
            ["A1"],
        )
        person = next(self.prepared.read_models("persons", Person))
        self.assertEqual(
            [authorship.publication_id for authorship in person.authored],
            ["W1"],
        )

    def test_same_doi_joins_two_sources(self):
        self.append("catalog", "P1", title="Catalog title", doi="https://doi.org/10.1/X")
        self.normalize("catalog")
        self.append("archive", "A1", title="Archive title", doi="doi:10.1/x")
        self.normalize("archive")
        publications = list(self.prepared.read_models("publications", Publication))
        self.assertEqual(len(publications), 1)
        self.assertEqual(set(publications[0].source_keys), {"catalog:P1", "archive:A1"})

    def test_concurrent_source_claim_repairs_provisional_doi_owner(self):
        resolver = IdentityResolver(self.db)
        original_claim = resolver.keys.claim

        def racing_claim(entity, identifier, entity_id):
            if identifier.normalized_key == "catalog:P1":
                original_claim(entity, identifier, "pub_winner")
            return original_claim(entity, identifier, entity_id)

        resolver.keys.claim = racing_claim
        owner = resolver.publication_id(IncomingPublication(
            source="catalog",
            source_id="P1",
            record_key="catalog:P1",
            title="Race",
            doi="10.1/race",
        ))
        self.assertEqual(owner, "pub_winner")
        self.assertEqual(
            {row["entity_id"] for row in self.db.identity_keys.find({})},
            {"pub_winner"},
        )

    def test_new_source_uses_deterministic_owner_of_legacy_duplicate_doi(self):
        self.prepared.write_models("publications", [
            Publication(
                id="W1", title="New preprint", type="preprint", doi="10.1/same",
                publication_date="2026-12-01",
            ),
            Publication(
                id="W2", title="Older article", type="article", doi="10.1/same",
                publication_date="2026-01-01",
            ),
        ])
        self.append("archive", "A1", title="Archive copy", doi="10.1/same")
        self.normalize("archive")
        owner = self.db.identity_keys.find_one({"_id": "publication:archive:A1"})
        self.assertEqual(owner["entity_id"], "W2")

    def test_v2_doi_resolution_uses_identity_keys_not_publication_scan(self):
        self.db.publications.insert_one({
            "_id": "pub_existing",
            "id": "pub_existing",
            "title": "Existing",
            "schema_version": 2,
            "doi": "10.1/existing",
        })
        resolver = IdentityResolver(self.db)
        self.assertIsNone(resolver._publication_by_doi("10.1/existing"))
        self.db.identity_keys.insert_one({
            "_id": "publication:doi:10.1/existing",
            "entity": "publication",
            "scheme": "doi",
            "normalized_value": "10.1/existing",
            "entity_id": "pub_existing",
        })
        owner = resolver.publication_id(IncomingPublication(
            source="archive",
            source_id="A1",
            record_key="archive:A1",
            title="Copy",
            doi="10.1/existing",
        ))
        self.assertEqual(owner, "pub_existing")

    def test_same_title_and_author_without_doi_is_review_only(self):
        author = {"name": "Ada", "orcid": "0000-0001-2345-6789"}
        self.append("catalog", "P1", title="Same title", authors=[author])
        self.normalize("catalog")
        self.append("archive", "A1", title="Same title", authors=[author])
        self.normalize("archive")
        self.assertEqual(self.db.publications.count_documents({}), 2)
        question = self.db.review_pairs.find_one({"kind": "publication_pair"})
        self.assertIsNotNone(question)

    def test_placeholder_titles_do_not_create_review_pairs(self):
        author = {"name": "Ada", "orcid": "0000-0001-2345-6789"}
        self.append("catalog", "P1", title="", authors=[author])
        self.append("catalog", "P2", title="Untitled", authors=[author])

        self.normalize("catalog")

        self.assertEqual(self.db.publications.count_documents({}), 2)
        self.assertEqual(
            self.db.review_pairs.count_documents({"kind": "publication_pair"}),
            0,
        )

    def test_confirmed_publication_review_merges_on_dedup(self):
        author = {"name": "Ada", "orcid": "0000-0001-2345-6789"}
        self.append("catalog", "P1", title="Same title", authors=[author])
        self.normalize("catalog")
        self.append("archive", "A1", title="Same title", authors=[author])
        self.normalize("archive")
        ids = sorted(self.db.publications.distinct("id"))
        review.record_verdict(
            self.db, review.PUBLICATION, ids, review.SAME, actor="test",
        )
        DedupStage(self.prepared, self.raw, self.config).run()
        self.assertEqual(self.db.publications.count_documents({}), 1)
        self.assertTrue(review.applied(self.db, review.PUBLICATION, ids))
        canonical = self.db.publications.find_one({})["id"]
        self.assertTrue(all(
            row["entity_id"] == canonical
            for row in self.db.identity_keys.find({"entity": "publication"})
        ))

    def test_conflicting_publication_review_chain_honours_negative_edge(self):
        self.prepared.write_models("publications", [
            Publication(id="pub_a", title="A", schema_version=2),
            Publication(id="pub_b", title="B", schema_version=2),
            Publication(id="pub_c", title="C", schema_version=2),
        ])
        review.record_verdict(
            self.db, review.PUBLICATION, ["pub_a", "pub_b"], review.SAME,
        )
        review.record_verdict(
            self.db, review.PUBLICATION, ["pub_b", "pub_c"], review.SAME,
        )
        review.record_verdict(
            self.db, review.PUBLICATION, ["pub_a", "pub_c"], review.DIFFERENT,
        )

        DedupStage(self.prepared, self.raw, self.config).run()
        DedupStage(self.prepared, self.raw, self.config).run()

        publications = list(self.prepared.read_models("publications", Publication))
        self.assertEqual(len(publications), 2)
        folded = next(row for row in publications if row.merged_ids)
        separate = next(row for row in publications if not row.merged_ids)
        self.assertEqual({folded.id, *folded.merged_ids}, {"pub_a", "pub_b"})
        self.assertEqual(separate.id, "pub_c")

    def test_person_dedup_repoints_identity_keys_and_source_authors(self):
        self.prepared.write_models("publications", [Publication(
            id="pub_1",
            title="Paper",
            schema_version=2,
            source_records=[{
                "source": "catalog",
                "source_id": "P1",
                "authors": [{"person_id": "per_b", "name": "Ada", "position": 1}],
            }],
        )])
        self.prepared.write_models("persons", [
            Person(
                id="per_a", schema_version=2, is_itmo=False, name_raw="Ada Lovelace",
                orcid="0000-0001-2345-6789",
                identifiers=[{"scheme": "orcid", "value": "0000-0001-2345-6789"}],
            ),
            Person(
                id="per_b", schema_version=2, is_itmo=False, name_raw="Ada Lovelace",
                orcid="0000-0001-2345-6789",
                authored=[{"publication_id": "pub_1", "position": 1}],
                identifiers=[{"scheme": "catalog", "value": "author-b"}],
            ),
        ])
        self.db.identity_keys.insert_one({
            "_id": "person:catalog:author-b",
            "entity": "person",
            "scheme": "catalog",
            "normalized_value": "author-b",
            "entity_id": "per_b",
        })

        DedupStage(self.prepared, self.raw, self.config).run()

        [person] = list(self.prepared.read_models("persons", Person))
        publication = next(self.prepared.read_models("publications", Publication))
        self.assertEqual(
            publication.source_records[0].authors[0].person_id,
            person.id,
        )
        self.assertEqual(
            self.db.identity_keys.find_one({"_id": "person:catalog:author-b"})["entity_id"],
            person.id,
        )

    def test_equal_names_without_identifiers_are_separate_occurrences(self):
        self.append("catalog", "P1", title="One", authors=[{"name": "Same Name"}])
        self.append("catalog", "P2", title="Two", authors=[{"name": "Same Name"}])
        self.normalize()
        self.assertEqual(self.db.persons.count_documents({}), 2)

    def test_equal_orcid_reuses_person(self):
        author = {"name": "Same Name", "orcid": "0000-0001-2345-6789"}
        self.append("catalog", "P1", title="One", authors=[author])
        self.append("catalog", "P2", title="Two", authors=[author])
        self.normalize()
        self.assertEqual(self.db.persons.count_documents({}), 1)

    def test_equal_source_author_id_reuses_person_and_is_persisted(self):
        author = {"name": "Same Name", "source_author_id": "author-42"}
        self.append("catalog", "P1", title="One", authors=[author])
        self.append("catalog", "P2", title="Two", authors=[author])
        self.normalize()
        self.assertEqual(self.db.persons.count_documents({}), 1)
        person = next(self.prepared.read_models("persons", Person))
        self.assertIn("catalog:author-42", {
            identifier.normalized_key for identifier in person.identifiers
        })

    def test_conflicting_author_identifiers_are_quarantined_for_review(self):
        self.prepared.write_models("persons", [
            Person(
                id="per_source", schema_version=2, is_itmo=False, name_raw="Ada Source",
                identifiers=[{"scheme": "catalog", "value": "author-42"}],
            ),
            Person(
                id="per_orcid", schema_version=2, is_itmo=False, name_raw="Ada ORCID",
                identifiers=[{"scheme": "orcid", "value": "0000-0001-2345-6789"}],
                orcid="0000-0001-2345-6789",
            ),
        ])
        self.append(
            "catalog", "P1", title="Conflict",
            authors=[{
                "name": "Ada",
                "source_author_id": "author-42",
                "orcid": "0000-0001-2345-6789",
            }],
        )

        self.normalize()

        publication = next(self.prepared.read_models("publications", Publication))
        [source_author] = publication.source_records[0].authors
        self.assertNotIn(source_author.person_id, {"per_source", "per_orcid"})
        self.assertEqual(
            {item.normalized_key for item in source_author.identifiers},
            {"catalog:author-42", "orcid:0000-0001-2345-6789"},
        )
        question = self.db.review_pairs.find_one({"kind": review.PAIR})
        self.assertEqual(set(question["members"]), {"per_source", "per_orcid"})
        self.normalize()
        self.assertEqual(self.db.persons.count_documents({}), 3)

    def test_openalex_identity_conflict_does_not_copy_stable_id_to_occurrence(self):
        self.prepared.write_models("persons", [
            Person(
                id="per_source", schema_version=2, is_itmo=False,
                identifiers=[{"scheme": "openalex", "value": "A1"}],
                openalex_id="A1",
            ),
            Person(
                id="per_orcid", schema_version=2, is_itmo=False,
                identifiers=[{"scheme": "orcid", "value": "0000-0001-2345-6789"}],
                orcid="0000-0001-2345-6789",
            ),
        ])
        raw = RawStore(self.db, "openalex-conflict")
        prepared = PreparedStore(self.db, "openalex-conflict")
        raw.append(
            "openalex_works",
            {
                "source_id": "W1",
                "title": "Conflict",
                "authors": [{
                    "name": "Ada",
                    "source_author_id": "A1",
                    "orcid": "0000-0001-2345-6789",
                }],
            },
            {},
            provider="openalex",
            record_key="openalex:W1",
            schema_version=2,
        )

        PublicationNormalizer(
            raw,
            prepared,
            AdapterRegistry([FakeAdapter("openalex")]),
        ).run("openalex")

        occurrence = self.db.persons.find_one({
            "id": {"$nin": ["per_source", "per_orcid"]},
        })
        self.assertIsNone(occurrence.get("openalex_id"))
        self.assertEqual(
            occurrence["identifiers"][0]["scheme"],
            "occurrence",
        )

    def test_normalize_preserves_enriched_authorship_affiliation(self):
        self.append(
            "catalog", "P1", title="Paper",
            authors=[{"name": "Ada", "source_author_id": "author-42"}],
        )
        self.normalize()
        person = next(self.prepared.read_models("persons", Person))
        person.authored[0].affiliation = "ITMO University"
        person.authored[0].affiliation_source = "orcid"
        person.processing["persons"] = ProcessingState(
            status=ProcessingStatus.COMPLETED,
        )
        self.prepared.write_models("persons", [person])

        self.normalize()

        person = next(self.prepared.read_models("persons", Person))
        self.assertEqual(person.authored[0].affiliation, "ITMO University")
        self.assertEqual(person.authored[0].affiliation_source, "orcid")
        self.assertEqual(
            person.processing["persons"].status,
            ProcessingStatus.COMPLETED,
        )

    def test_concurrent_author_claim_repairs_provisional_identifier_owner(self):
        resolver = IdentityResolver(self.db)
        original_claim = resolver.keys.claim

        def racing_claim(entity, identifier, entity_id):
            if identifier.normalized_key == "catalog:author-42":
                original_claim(entity, identifier, "per_winner")
            return original_claim(entity, identifier, entity_id)

        resolver.keys.claim = racing_claim
        owner = resolver.person_id(
            IncomingAuthor(
                source_author_id="author-42",
                name="Ada",
                orcid="0000-0001-2345-6789",
                position=1,
            ),
            "catalog",
            "catalog:P1",
        )
        self.assertEqual(owner, "per_winner")
        self.assertEqual(
            {row["entity_id"] for row in self.db.identity_keys.find({})},
            {"per_winner"},
        )

    def test_occurrence_identity_is_persisted_and_source_correction_resets_person(self):
        self.append("catalog", "P1", title="One", authors=[{"name": "Old Name"}])
        self.normalize()
        person = next(self.prepared.read_models("persons", Person))
        original_id = person.id
        person.processing["persons"] = ProcessingState(status=ProcessingStatus.COMPLETED)
        self.prepared.write_models("persons", [person])
        self.append("catalog", "P1", title="One", authors=[{"name": "Corrected Name"}])
        self.normalize()
        corrected = next(self.prepared.read_models("persons", Person))
        self.assertEqual(corrected.id, original_id)
        self.assertEqual(corrected.name_raw, "Corrected Name")
        self.assertEqual(corrected.processing, {})
        self.assertEqual(
            corrected.identifiers[0].normalized_key,
            "occurrence:catalog:P1:1",
        )

    def test_occurrence_is_upgraded_when_source_later_supplies_orcid(self):
        self.append("catalog", "P1", title="One", authors=[{"name": "Ada"}])
        self.normalize()
        original_id = next(self.prepared.read_models("persons", Person)).id
        self.append(
            "catalog", "P1", title="One",
            authors=[{"name": "Ada", "orcid": "0000-0001-2345-6789"}],
        )
        self.normalize()
        people = list(self.prepared.read_models("persons", Person))
        self.assertEqual([person.id for person in people], [original_id])
        self.assertEqual(people[0].orcid, "0000-0001-2345-6789")

    def test_unknown_provider_fails_before_prepared_writes(self):
        self.raw.append(
            "mystery_publications", {"source_id": "M1"}, {},
            provider="mystery", record_key="mystery:M1", schema_version=2,
        )
        with self.assertRaisesRegex(ValueError, "unknown raw provider"):
            self.normalize("mystery")
        self.assertEqual(self.db.publications.count_documents({}), 0)
        self.assertEqual(self.db.identity_keys.count_documents({}), 0)

    def test_unknown_empty_provider_also_fails_before_prepared_writes(self):
        with self.assertRaisesRegex(ValueError, "unknown raw provider"):
            self.normalize("mystery")
        self.assertEqual(self.db.publications.count_documents({}), 0)
        self.assertEqual(self.db.identity_keys.count_documents({}), 0)


if __name__ == "__main__":
    unittest.main()
