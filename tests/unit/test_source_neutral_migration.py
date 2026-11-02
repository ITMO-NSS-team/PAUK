import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mongomock

from pauk.migrations.source_neutral import MigrationBlocked, SourceNeutralMigration


class SourceNeutralMigrationTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().db

    def test_apply_backfills_and_is_idempotent(self):
        self.db.publications.insert_one({
            "_id": "W1", "id": "W1", "title": "Paper", "doi": "https://doi.org/10.1/X",
            "openalex_url": "https://openalex.org/W1", "groups": ["g"], "_processing": {"x": {}},
        })
        migration = SourceNeutralMigration(self.db)
        first = migration.apply()
        publication = self.db.publications.find_one({"_id": "W1"})
        self.assertEqual(publication["schema_version"], 2)
        self.assertEqual(publication["source_keys"], ["openalex:W1"])
        self.assertEqual(publication["groups"], ["g"])
        self.assertIn("x", publication["_processing"])
        self.assertEqual(self.db.identity_keys.find_one({"_id": "publication:openalex:W1"})["entity_id"], "W1")
        second = migration.apply()
        self.assertTrue(any(first.values()))
        self.assertFalse(any(second.values()))

    def test_doi_merge_repoints_every_prepared_reference(self):
        for publication_id in ("W1", "W2"):
            self.db.publications.insert_one({
                "_id": publication_id, "id": publication_id,
                "title": publication_id, "doi": "10.1/same", "groups": [publication_id],
            })
        self.db.persons.insert_one({
            "_id": "A1", "id": "A1", "is_itmo": False,
            "authored": [{"publication_id": "W2", "position": 1}], "groups": ["g"],
        })
        self.db.repo_links.insert_one({
            "_id": "W2", "publication_id": "W2", "links": [], "groups": ["g"],
        })
        self.db.repositories.insert_one({
            "_id": "R1", "id": "R1", "name": "r", "url": "u",
            "publication_ids": ["W2"], "groups": ["g"],
        })
        SourceNeutralMigration(self.db).apply()
        self.assertEqual(self.db.publications.count_documents({}), 1)
        canonical = self.db.publications.find_one({})["id"]
        self.assertEqual(self.db.persons.find_one({})["authored"][0]["publication_id"], canonical)
        self.assertEqual(self.db.repo_links.find_one({})["publication_id"], canonical)
        self.assertEqual(self.db.repositories.find_one({})["publication_ids"], [canonical])

    def test_doi_merge_rebuilds_canonical_fields_from_ranked_source_records(self):
        self.db.publications.insert_many([
            {
                "_id": "W1", "id": "W1", "title": "Article title", "type": "article",
                "doi": "10.1/same", "groups": ["g"],
            },
            {
                "_id": "W2", "id": "W2", "title": "Preprint title", "type": "preprint",
                "doi": "10.1/same", "abstract": "Available abstract", "groups": ["g"],
            },
        ])
        SourceNeutralMigration(self.db).apply()
        publication = self.db.publications.find_one({})
        self.assertEqual(publication["id"], "W1")
        self.assertEqual(publication["title"], "Article title")
        self.assertEqual(publication["abstract"], "Available abstract")
        self.assertEqual(len(publication["source_records"]), 2)

    def test_doi_merge_preserves_enrichment_from_removed_duplicate(self):
        self.db.publications.insert_many([
            {
                "_id": "W1", "id": "W1", "title": "Article", "type": "article",
                "doi": "10.1/same", "groups": ["g"],
            },
            {
                "_id": "W2", "id": "W2", "title": "Preprint", "type": "preprint",
                "doi": "10.1/same", "groups": ["g"],
                "has_code": True,
                "code_url": '["https://github.com/org/repo"]',
                "full_text": "parsed PDF",
                "department_ids": ["D1"],
                "mentions_links": [{
                    "target_kind": "candidate",
                    "candidate_id": "https://github.com/org/repo",
                }],
                "_processing": {"code_links": {"status": "completed"}},
            },
        ])

        SourceNeutralMigration(self.db).apply()

        publication = self.db.publications.find_one({})
        self.assertEqual(publication["id"], "W1")
        self.assertTrue(publication["has_code"])
        self.assertEqual(publication["full_text"], "parsed PDF")
        self.assertEqual(publication["department_ids"], ["D1"])
        self.assertEqual(len(publication["mentions_links"]), 1)
        self.assertEqual(publication["_processing"]["code_links"]["status"], "completed")

    def test_interrupted_doi_merge_persists_survivor_before_delete(self):
        self.db.publications.insert_many([
            {
                "_id": "W1", "id": "W1", "title": "Article", "type": "article",
                "doi": "10.1/same", "groups": ["g"],
            },
            {
                "_id": "W2", "id": "W2", "title": "Preprint", "type": "preprint",
                "doi": "10.1/same", "groups": ["g"], "full_text": "preserve me",
            },
        ])
        migration = SourceNeutralMigration(self.db)

        with patch.object(
            self.db.publications,
            "delete_one",
            side_effect=RuntimeError("interrupted"),
        ), self.assertRaisesRegex(RuntimeError, "interrupted"):
            migration.apply()

        survivor = self.db.publications.find_one({"id": "W1"})
        self.assertEqual(survivor["full_text"], "preserve me")
        self.assertIn("W2", survivor["merged_ids"])
        self.assertIsNotNone(self.db.publications.find_one({"id": "W2"}))

        migration.apply()

        self.assertEqual(self.db.publications.count_documents({}), 1)
        self.assertEqual(
            self.db.publications.find_one({})["full_text"],
            "preserve me",
        )

    def test_backfill_copies_legacy_authors_into_source_record(self):
        self.db.publications.insert_one({
            "_id": "W1", "id": "W1", "title": "Paper", "groups": ["g"],
        })
        self.db.persons.insert_one({
            "_id": "A1", "id": "A1", "name_raw": "Ada", "is_itmo": False,
            "authored": [{"publication_id": "W1", "position": 1}], "groups": ["g"],
        })

        SourceNeutralMigration(self.db).apply()

        publication = self.db.publications.find_one({})
        self.assertEqual(
            publication["source_records"][0]["authors"],
            [{
                "person_id": "A1",
                "name": "Ada",
                "position": 1,
                "identifiers": [],
                "affiliation": None,
                "is_corresponding": False,
            }],
        )

    def test_doi_components_include_every_source_record_and_keep_assigned_owner(self):
        rows = [
            ("W1", [{"key": "catalog:P1", "source": "catalog", "source_id": "P1",
                     "title": "Article", "type": "article", "doi": "10.1/a"}]),
            ("W2", [
                {"key": "archive:A1", "source": "archive", "source_id": "A1",
                 "title": "Bridge", "doi": "10.1/a"},
                {"key": "catalog:P2", "source": "catalog", "source_id": "P2",
                 "title": "Bridge", "doi": "10.1/b"},
            ]),
            ("W3", [{"key": "archive:A2", "source": "archive", "source_id": "A2",
                     "title": "Preprint", "type": "preprint", "doi": "10.1/b",
                     "abstract": "Fallback abstract"}]),
        ]
        for publication_id, records in rows:
            self.db.publications.insert_one({
                "_id": publication_id,
                "id": publication_id,
                "title": records[0]["title"],
                "source_records": records,
                "groups": ["g"],
            })
        self.db.identity_keys.insert_one({
            "_id": "publication:doi:10.1/b",
            "entity": "publication",
            "scheme": "doi",
            "normalized_value": "10.1/b",
            "entity_id": "W3",
        })
        self.db.identity_keys.insert_one({
            "_id": "publication:catalog:P1",
            "entity": "publication",
            "scheme": "catalog",
            "normalized_value": "P1",
            "entity_id": "W1",
        })
        plan = SourceNeutralMigration(self.db).plan()
        [component] = plan["doi_components"].values()
        self.assertEqual(component["members"], ["W1", "W2", "W3"])
        self.assertEqual(component["canonical"], "W3")
        SourceNeutralMigration(self.db).apply()
        publication = self.db.publications.find_one({})
        self.assertEqual(publication["id"], "W3")
        self.assertEqual(len(publication["source_records"]), 4)
        self.assertEqual(publication["abstract"], "Fallback abstract")
        self.assertTrue(all(
            row["entity_id"] == "W3"
            for row in self.db.identity_keys.find({"entity": "publication"})
        ))

    def test_apply_blocks_on_preexisting_dangling_reference(self):
        self.db.persons.insert_one({
            "_id": "A1", "id": "A1", "is_itmo": False,
            "authored": [{"publication_id": "missing", "position": 1}], "groups": ["g"],
        })
        with self.assertRaisesRegex(MigrationBlocked, "dangling"):
            SourceNeutralMigration(self.db).apply()

    def test_apply_blocks_source_key_collisions_before_writing(self):
        for publication_id in ("W1", "W2"):
            self.db.publications.insert_one({
                "_id": publication_id,
                "id": publication_id,
                "title": publication_id,
                "source_records": [{
                    "key": "catalog:same",
                    "source": "catalog",
                    "source_id": "same",
                    "title": publication_id,
                }],
                "groups": ["g"],
            })
        migration = SourceNeutralMigration(self.db)
        self.assertIn("catalog:same", migration.plan()["source_key_collisions"])
        with self.assertRaisesRegex(MigrationBlocked, "source record"):
            migration.apply()
        self.assertTrue(all(
            "schema_version" not in row for row in self.db.publications.find({})
        ))

    def test_source_collision_inside_planned_doi_merge_is_resolved(self):
        self.db.publications.insert_many([
            {
                "_id": "W1", "id": "W1", "title": "Old", "doi": "10.1/old",
                "groups": ["old"],
            },
            {
                "_id": "W2", "id": "W2", "title": "New", "doi": "10.1/new",
                "merged_ids": ["W1"], "groups": ["new"],
                "source_records": [
                    {
                        "key": "openalex:W2", "source": "openalex", "source_id": "W2",
                        "title": "New", "doi": "10.1/new",
                    },
                    {
                        "key": "openalex:W1", "source": "openalex", "source_id": "W1",
                        "title": "Old rich", "doi": "10.1/old", "abstract": "Preserve me",
                        "authors": [{"person_id": "A1", "name": "Ada", "position": 1}],
                    },
                ],
            },
        ])
        self.db.persons.insert_one({"_id": "A1", "id": "A1", "is_itmo": False})
        migration = SourceNeutralMigration(self.db)
        plan = migration.plan()
        self.assertIn("openalex:W1", plan["source_key_collisions"])
        self.assertIn("openalex:W1", plan["resolvable_source_key_collisions"])
        self.assertEqual(plan["blocking_source_key_collisions"], {})
        migration.apply()
        publication = self.db.publications.find_one({})
        self.assertEqual(publication["id"], "W2")
        old_record = next(
            item for item in publication["source_records"] if item["key"] == "openalex:W1"
        )
        self.assertEqual(old_record["abstract"], "Preserve me")
        self.assertEqual(old_record["authors"][0]["person_id"], "A1")
        self.assertEqual(
            self.db.identity_keys.find_one({"_id": "publication:openalex:W1"})["entity_id"],
            "W2",
        )

    def test_active_override_on_removed_doi_duplicate_blocks_apply(self):
        self.db.publications.insert_many([
            {"_id": "W1", "id": "W1", "title": "Article", "type": "article",
             "doi": "10.1/same", "groups": ["g"]},
            {"_id": "W2", "id": "W2", "title": "Preprint", "type": "preprint",
             "doi": "10.1/same", "groups": ["g"]},
        ])
        self.db.graph_overrides.insert_one({
            "_id": "node:Publication:W2",
            "active": True,
            "label": "Publication",
            "target_id": "W2",
        })
        with self.assertRaisesRegex(MigrationBlocked, "overrides"):
            SourceNeutralMigration(self.db).apply()

    def test_duplicate_person_identity_is_merged_and_references_are_repointed(self):
        self.db.publications.insert_one({
            "_id": "W1", "id": "W1", "title": "Paper", "groups": ["g"],
            "source_records": [{
                "key": "catalog:P1", "source": "catalog", "source_id": "P1",
                "title": "Paper",
                "authors": [{"person_id": "A2", "name": "Ada", "position": 1}],
            }],
        })
        self.db.persons.insert_many([
            {
                "_id": "A1", "id": "A1", "is_itmo": True, "orcid": "0000-0001-0000-0001",
                "authored": [{"publication_id": "W1", "position": 1}],
                "merged_ids": ["A2"], "groups": ["g1"],
            },
            {
                "_id": "A2", "id": "A2", "is_itmo": False, "orcid": "0000-0001-0000-0001",
                "authored": [{
                    "publication_id": "W1", "position": 1,
                    "affiliation": "ITMO", "is_corresponding": True,
                }],
                "groups": ["g2"],
            },
        ])
        migration = SourceNeutralMigration(self.db)
        plan = migration.plan()
        self.assertEqual(plan["person_identity_components"], [{
            "members": ["A1", "A2"], "canonical": "A1",
        }])
        migration.apply()
        person = self.db.persons.find_one({})
        self.assertEqual(person["id"], "A1")
        self.assertEqual(set(person["groups"]), {"g1", "g2"})
        self.assertIn("A2", person["merged_ids"])
        self.assertEqual(len(person["authored"]), 1)
        self.assertEqual(person["authored"][0]["affiliation"], "ITMO")
        self.assertTrue(person["authored"][0]["is_corresponding"])
        publication = self.db.publications.find_one({})
        self.assertEqual(
            publication["source_records"][0]["authors"][0]["person_id"],
            "A1",
        )
        self.assertEqual(
            self.db.identity_keys.find_one({"_id": "person:openalex:A2"})["entity_id"],
            "A1",
        )
        self.assertEqual(self.db.person_merge_sources.count_documents({}), 2)

    def test_interrupted_person_merge_persists_survivor_before_delete(self):
        self.db.persons.insert_many([
            {
                "_id": "A1", "id": "A1", "is_itmo": True,
                "orcid": "0000-0001-0000-0001", "merged_ids": ["A2"],
                "groups": ["g1"],
            },
            {
                "_id": "A2", "id": "A2", "is_itmo": False,
                "orcid": "0000-0001-0000-0001", "emails": ["kept@example.org"],
                "groups": ["g2"],
            },
        ])
        migration = SourceNeutralMigration(self.db)

        with patch.object(
            self.db.persons,
            "delete_one",
            side_effect=RuntimeError("interrupted"),
        ), self.assertRaisesRegex(RuntimeError, "interrupted"):
            migration.apply()

        survivor = self.db.persons.find_one({"id": "A1"})
        self.assertEqual(survivor["emails"], ["kept@example.org"])
        self.assertIn("A2", survivor["merged_ids"])
        self.assertIsNotNone(self.db.persons.find_one({"id": "A2"}))

        migration.apply()

        self.assertEqual(self.db.persons.count_documents({}), 1)
        self.assertEqual(
            self.db.persons.find_one({})["emails"],
            ["kept@example.org"],
        )

    def test_active_override_on_removed_person_duplicate_blocks_apply(self):
        self.db.persons.insert_many([
            {
                "_id": "A1", "id": "A1", "is_itmo": False,
                "orcid": "0000-0001-0000-0001", "authored": [{"publication_id": "W1"}],
                "groups": ["g"],
            },
            {
                "_id": "A2", "id": "A2", "is_itmo": False,
                "orcid": "0000-0001-0000-0001", "groups": ["g"],
            },
        ])
        self.db.graph_overrides.insert_one({
            "_id": "node:Person:A2",
            "active": True,
            "label": "Person",
            "target_id": "A2",
        })
        with self.assertRaisesRegex(MigrationBlocked, "person duplicates"):
            SourceNeutralMigration(self.db).apply()

    def test_historical_graph_canonical_expands_exact_identity_component(self):
        self.db.persons.insert_many([
            {
                "_id": "A0", "id": "A0", "is_itmo": True,
                "name_raw": "Ada Lovelace", "groups": ["old"],
            },
            {
                "_id": "A1", "id": "A1", "is_itmo": False,
                "orcid": "0000-0001-0000-0001", "groups": ["new"],
            },
            {
                "_id": "A2", "id": "A2", "is_itmo": False,
                "orcid": "0000-0001-0000-0001", "groups": ["new"],
            },
        ])
        migration = SourceNeutralMigration(
            self.db,
            person_aliases={"A2": "A0"},
        )

        self.assertEqual(migration.plan()["person_identity_components"], [{
            "members": ["A0", "A1", "A2"], "canonical": "A0",
        }])
        migration.apply()

        person = self.db.persons.find_one({})
        self.assertEqual(person["id"], "A0")
        self.assertEqual(set(person["merged_ids"]), {"A1", "A2"})
        self.assertEqual(set(person["groups"]), {"old", "new"})
        self.assertEqual(
            self.db.identity_keys.find_one({"_id": "person:orcid:0000-0001-0000-0001"})[
                "entity_id"
            ],
            "A0",
        )

    def test_competing_historical_graph_canonicals_fold_deterministically(self):
        self.db.persons.insert_many([
            {"_id": "R1", "id": "R1", "is_itmo": False},
            {"_id": "R2", "id": "R2", "is_itmo": False},
            {
                "_id": "A1", "id": "A1", "is_itmo": False,
                "orcid": "0000-0001-0000-0001",
            },
            {
                "_id": "A2", "id": "A2", "is_itmo": False,
                "orcid": "0000-0001-0000-0001",
            },
        ])
        migration = SourceNeutralMigration(
            self.db,
            person_aliases={"A1": "R1", "A2": "R2", "old-R1": "R1"},
        )

        self.assertEqual(migration.plan()["person_identity_components"], [{
            "members": ["A1", "A2", "R1", "R2"], "canonical": "R1",
        }])
        migration.apply()
        person = self.db.persons.find_one({})
        self.assertEqual(person["id"], "R1")
        self.assertEqual(set(person["merged_ids"]), {"A1", "A2", "R2"})

    def test_historical_graph_alias_repoints_source_author_without_person_row(self):
        self.db.persons.insert_one({
            "_id": "A0", "id": "A0", "is_itmo": True, "groups": ["g"],
        })
        self.db.publications.insert_one({
            "_id": "W1", "id": "W1", "title": "Paper", "groups": ["g"],
            "source_records": [{
                "key": "catalog:P1", "source": "catalog", "source_id": "P1",
                "authors": [{"person_id": "A-old", "name": "Ada", "position": 1}],
            }],
        })
        migration = SourceNeutralMigration(
            self.db,
            person_aliases={"A-old": "A0"},
        )

        plan = migration.plan()
        self.assertEqual(plan["resolvable_source_author_aliases"], 1)
        self.assertEqual(plan["dangling_references"]["source_authors"], 0)
        result = migration.apply()

        self.assertEqual(result["repoint_graph_person_aliases"], 1)
        publication = self.db.publications.find_one({})
        self.assertEqual(publication["source_records"][0]["authors"][0]["person_id"], "A0")

    def test_unresolved_source_author_blocks_apply(self):
        self.db.publications.insert_one({
            "_id": "W1", "id": "W1", "title": "Paper", "groups": ["g"],
            "source_records": [{
                "key": "catalog:P1", "source": "catalog", "source_id": "P1",
                "authors": [{"person_id": "missing", "name": "Ada", "position": 1}],
            }],
        })
        migration = SourceNeutralMigration(self.db)

        self.assertEqual(migration.plan()["dangling_references"]["source_authors"], 1)
        with self.assertRaisesRegex(MigrationBlocked, "dangling"):
            migration.apply()

    def test_graph_only_manual_entities_are_imported_as_source_records(self):
        migration = SourceNeutralMigration(
            self.db,
            graph_persons=[{
                "properties": {
                    "id": "manual_person_ada", "is_itmo": True, "name_en": "Ada",
                },
                "authored": [{"publication_id": "manual_workshop_1", "position": 1}],
            }],
            graph_publications=[{
                "properties": {
                    "id": "manual_workshop_1", "title": "Workshop paper",
                    "type": "conference-paper", "year": 2026,
                },
                "authors": [{
                    "person_id": "manual_person_ada", "name": "Ada", "position": 1,
                }],
            }],
        )

        plan = migration.plan()
        self.assertEqual(plan["graph_only_persons"], ["manual_person_ada"])
        self.assertEqual(plan["graph_only_publications"], ["manual_workshop_1"])
        result = migration.apply()

        self.assertEqual(result["import_graph_legacy_entities"], 2)
        person = self.db.persons.find_one({"id": "manual_person_ada"})
        self.assertEqual(person["groups"], ["legacy-graph"])
        self.assertEqual(person["authored"][0]["publication_id"], "manual_workshop_1")
        publication = self.db.publications.find_one({"id": "manual_workshop_1"})
        self.assertEqual(publication["source_records"][0]["source"], "manual")
        self.assertEqual(
            publication["source_records"][0]["metadata"]["migrated_from"],
            "neo4j",
        )
        self.assertEqual(
            self.db.identity_keys.find_one({"_id": "publication:manual:manual_workshop_1"})[
                "entity_id"
            ],
            "manual_workshop_1",
        )
        self.assertEqual(
            self.db.identity_keys.find_one({"_id": "person:manual:manual_person_ada"})[
                "entity_id"
            ],
            "manual_person_ada",
        )

    def test_graph_json_text_fields_are_decoded_before_validation(self):
        migration = SourceNeutralMigration(
            self.db,
            graph_persons=[{
                "properties": {
                    "id": "manual_person_ada",
                    "is_itmo": True,
                    "affiliations": '[{"name":"ITMO","source":"manual"}]',
                    "counts_by_year": '{"2026": 1}',
                },
            }],
            graph_publications=[{
                "properties": {
                    "id": "manual_workshop_1",
                    "title": "Workshop paper",
                    "funding": '[{"funder":"ITMO"}]',
                },
                "authors": [],
            }],
        )

        migration.plan()
        migration.apply()

        person = self.db.persons.find_one({"id": "manual_person_ada"})
        publication = self.db.publications.find_one({"id": "manual_workshop_1"})
        self.assertEqual(person["affiliations"][0]["name"], "ITMO")
        self.assertEqual(person["counts_by_year"], {"2026": 1})
        self.assertEqual(publication["funding"][0]["funder"], "ITMO")

    def test_graph_only_publication_with_unknown_author_blocks_apply(self):
        migration = SourceNeutralMigration(
            self.db,
            graph_publications=[{
                "properties": {"id": "manual_workshop_1", "title": "Workshop paper"},
                "authors": [{"person_id": "missing", "position": 1}],
            }],
        )

        self.assertEqual(migration.plan()["graph_legacy_unresolved_authors"], [{
            "publication": "manual_workshop_1", "person": "missing",
        }])
        with self.assertRaisesRegex(MigrationBlocked, "unresolved authors"):
            migration.apply()

    def test_apply_creates_pointers_for_unindexed_legacy_pdfs(self):
        with tempfile.TemporaryDirectory() as temporary:
            pdf_dir = Path(temporary)
            (pdf_dir / "W77.pdf").write_bytes(b"pdf")
            migration = SourceNeutralMigration(self.db, pdf_dir)
            self.assertEqual(migration.plan()["pdf_without_pointer"], 1)
            migration.apply()
            self.assertEqual(
                self.db.pdfs.find_one({"_id": "openalex:W77"})["filename"],
                "W77.pdf",
            )


if __name__ == "__main__":
    unittest.main()
