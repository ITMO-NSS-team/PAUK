import tempfile
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from pauk.graph.jsonl_loader import load_prepared_rows
from pauk.graph.load import ENTITY_FILES
from pauk.graph.unmerge import NothingToRebuild, rebuildable, split_person
from pauk.ingest.contracts import IncomingAuthor
from pauk.ingest.identity import IdentityResolver
from pauk.models import Publication
from pauk.pipeline.stages.dedup import DedupStage
from pauk.settings import Settings
from pauk.storage import PreparedStore, RawStore
from pauk.storage.person_merge_sources import COLLECTION, preserve
from pauk.storage.prepared import trim_revisions
from tests.unit.test_unmerge import UnmergeTest, person


class ArchivedPersonMergeTest(UnmergeTest):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.stage = DedupStage(
            self.prepared, RawStore(self.db, self.prepared.group),
            Settings(data_dir=Path(temporary.name), person_resolution_enabled=False),
        )

    def republish(self):
        load_prepared_rows(self.graph, {
            filename: list(self.prepared.read_rows(entity)) for entity, filename in ENTITY_FILES.items()
        })

    def prepared_fold(self):
        self.stage._dedup_persons()
        self.republish()

    def seed_pair(self):
        self.publish(
            person("A1", "Ivan Smirnov", works=["W1"], orcid="0000-1", other_names=["Original A"]),
            person("A2", "I. Smirnov", works=["W2"], orcid="0000-1", other_names=["Original B"]),
        )

    def test_deleted_prepared_person_is_rebuilt_and_republish_keeps_the_split(self):
        self.seed_pair()
        self.prepared_fold()
        self.assertIsNone(self.db.persons.find_one({"id": "A2"}))
        self.assertEqual(rebuildable(self.db, ["A1", "A2"]), {"A1", "A2"})
        split_person(self.graph, self.db, ["A1", "A2"])
        self.assertEqual(self.edges("A1"), {("AUTHORED", "W1")})
        self.assertEqual(self.edges("A2"), {("AUTHORED", "W2")})
        rows = {row["id"]: row for row in self.prepared.read_rows("persons")}
        self.assertEqual(rows["A1"]["merged_ids"], [])
        self.assertEqual(rows["A1"]["other_names"], ["Original A"])
        self.assertEqual(rows["A2"]["other_names"], ["Original B"])
        self.republish()
        self.assertEqual(self.edges("A1"), {("AUTHORED", "W1")})
        self.assertEqual(self.edges("A2"), {("AUTHORED", "W2")})

    def test_revision_trimming_does_not_remove_undo_sources(self):
        self.seed_pair()
        self.prepared_fold()
        trim_revisions(self.db, "9999", apply=True)
        self.assertEqual(self.db.revisions.count_documents({}), 0)
        self.assertEqual(self.db[COLLECTION].count_documents({}), 2)
        split_person(self.graph, self.db, ["A1", "A2"])
        self.assertIn(("Person", "A2"), self.graph.nodes)

    def test_repeated_preservation_is_idempotent(self):
        self.seed_pair()
        preserve(self.db, ["A1", "A2"])
        preserve(self.db, ["A2", "A1"])
        self.assertEqual(self.db[COLLECTION].count_documents({}), 2)

    def test_failed_preservation_prevents_prepared_mutations(self):
        self.seed_pair()
        before = list(self.db.persons.find().sort("id"))
        graph_before = deepcopy((self.graph.nodes, self.graph.relationships))
        with (
            patch.object(self.db[COLLECTION], "update_one", side_effect=RuntimeError("archive unavailable")),
            self.assertRaisesRegex(RuntimeError, "archive unavailable"),
        ):
            self.stage._dedup_persons()
        self.assertEqual(list(self.db.persons.find().sort("id")), before)
        self.assertEqual((self.graph.nodes, self.graph.relationships), graph_before)

    def test_missing_participant_prevents_partial_archiving(self):
        self.seed_pair()
        with self.assertRaisesRegex(ValueError, "cannot preserve every person"):
            preserve(self.db, ["A1", "missing"])
        self.assertEqual(self.db[COLLECTION].count_documents({}), 0)

    def test_missing_survivor_source_refuses_before_mutation(self):
        self.seed_pair()
        self.prepared_fold()
        self.db[COLLECTION].delete_many({"person_id": "A1"})
        before = deepcopy((self.graph.nodes, self.graph.relationships))
        prepared_before = list(self.db.persons.find())
        with self.assertRaises(NothingToRebuild):
            split_person(self.graph, self.db, ["A1", "A2"])
        self.assertEqual((self.graph.nodes, self.graph.relationships), before)
        self.assertEqual(list(self.db.persons.find()), prepared_before)

    def test_archived_authorship_flags_and_emails_return_to_their_owners(self):
        first = person("A1", "Ivan Smirnov", works=["W1", "W2"], orcid="0000-1", emails=["a@example.org"])
        second = person("A2", "I. Smirnov", works=["W1"], orcid="0000-1", emails=["b@example.org"])
        first.authored[0].is_corresponding = False
        second.authored[0].is_corresponding = True
        self.publish(first, second)
        self.prepared_fold()
        split_person(self.graph, self.db, ["A1", "A2"])
        self.republish()
        for identifier, corresponding, email in (("A1", False, "a@example.org"), ("A2", True, "b@example.org")):
            self.assertEqual(self.graph.nodes[("Person", identifier)]["emails"], [email])
            edge = self.graph.relationships[("Person", "AUTHORED", "Publication", identifier, "W1")]
            self.assertEqual(edge["is_corresponding"], corresponding)

    def test_split_restores_identity_keys_and_source_record_authors(self):
        first = person("A1", "Ivan Smirnov", works=["W1"], orcid="0000-1")
        second = person("A2", "I. Smirnov", works=["W2"], orcid="0000-1")
        self.publish(first, second)
        self.prepared.write_models("publications", [
            Publication(
                id=publication_id,
                title=publication_id,
                schema_version=2,
                source_records=[{
                    "key": f"openalex:{publication_id}",
                    "source": "openalex",
                    "source_id": publication_id,
                    "title": publication_id,
                    "authors": [{
                        "person_id": person_id,
                        "name": name,
                        "position": 1,
                        "identifiers": [{"scheme": "openalex", "value": person_id}],
                    }],
                }],
            )
            for publication_id, person_id, name in (
                ("W1", "A1", "Ivan Smirnov"),
                ("W2", "A2", "I. Smirnov"),
            )
        ])
        self.db.identity_keys.insert_many([
            {
                "_id": f"person:openalex:{person_id}",
                "entity": "person",
                "scheme": "openalex",
                "normalized_value": person_id,
                "entity_id": person_id,
            }
            for person_id in ("A1", "A2")
        ])

        self.prepared_fold()
        self.assertEqual(
            self.db.identity_keys.find_one({"_id": "person:openalex:A2"})["entity_id"],
            "A1",
        )
        folded = self.db.publications.find_one({"id": "W2"})
        self.assertEqual(folded["source_records"][0]["authors"][0]["person_id"], "A1")
        self.assertEqual(folded["versions"][0]["authors"][0]["person_id"], "A1")

        split_person(self.graph, self.db, ["A1", "A2"])

        self.assertEqual(
            self.db.identity_keys.find_one({"_id": "person:openalex:A2"})["entity_id"],
            "A2",
        )
        restored = self.db.publications.find_one({"id": "W2"})
        self.assertEqual(restored["source_records"][0]["authors"][0]["person_id"], "A2")
        self.assertEqual(restored["versions"][0]["authors"][0]["person_id"], "A2")
        resolved = IdentityResolver(self.db).person_id(
            IncomingAuthor(
                source_author_id="A2",
                name="I. Smirnov",
                position=1,
                identifiers=[{"scheme": "openalex", "value": "A2"}],
            ),
            "openalex",
            "openalex:W2",
        )
        self.assertEqual(resolved, "A2")

    def test_identity_owner_conflict_refuses_before_restoring_prepared_rows(self):
        self.seed_pair()
        self.prepared_fold()
        self.db.identity_keys.insert_one({
            "_id": "person:openalex:A2",
            "entity": "person",
            "scheme": "openalex",
            "normalized_value": "A2",
            "entity_id": "per_third_party",
        })
        graph_before = deepcopy((self.graph.nodes, self.graph.relationships))
        prepared_before = list(self.db.persons.find())

        with self.assertRaisesRegex(NothingToRebuild, "owned by per_third_party"):
            split_person(self.graph, self.db, ["A1", "A2"])

        self.assertEqual((self.graph.nodes, self.graph.relationships), graph_before)
        self.assertEqual(list(self.db.persons.find()), prepared_before)

    def test_interrupted_graph_split_can_resume_while_fold_marker_remains(self):
        self.seed_pair()
        self.prepared_fold()

        with patch(
            "pauk.graph.unmerge._take_off",
            side_effect=RuntimeError("interrupted"),
        ), self.assertRaisesRegex(RuntimeError, "interrupted"):
            split_person(self.graph, self.db, ["A1", "A2"])

        self.assertIn(("Person", "A2"), self.graph.nodes)
        self.assertIn("A2", self.graph.nodes[("Person", "A1")]["merged_ids"])

        split_person(self.graph, self.db, ["A1", "A2"])

        self.assertNotIn("A2", self.graph.nodes[("Person", "A1")]["merged_ids"])
        self.assertEqual(self.edges("A1"), {("AUTHORED", "W1")})
        self.assertEqual(self.edges("A2"), {("AUTHORED", "W2")})

    def test_override_failure_leaves_graph_split_resumable(self):
        self.seed_pair()
        self.prepared_fold()

        with patch(
            "pauk.graph.unmerge.apply_overrides",
            side_effect=RuntimeError("override interrupted"),
        ), self.assertRaisesRegex(RuntimeError, "override interrupted"):
            split_person(self.graph, self.db, ["A1", "A2"])

        self.assertIn(("Person", "A2"), self.graph.nodes)
        self.assertIn("A2", self.graph.nodes[("Person", "A1")]["merged_ids"])

        split_person(self.graph, self.db, ["A1", "A2"])

        self.assertNotIn("A2", self.graph.nodes[("Person", "A1")]["merged_ids"])
        self.assertEqual(self.edges("A1"), {("AUTHORED", "W1")})
        self.assertEqual(self.edges("A2"), {("AUTHORED", "W2")})

    def test_later_enrichment_is_not_silently_replaced_by_an_old_snapshot(self):
        self.seed_pair()
        self.prepared_fold()
        self.db.persons.update_one({"id": "A1"}, {"$addToSet": {"emails": "new@example.org"}})
        before = deepcopy((self.graph.nodes, self.graph.relationships))
        prepared_before = list(self.db.persons.find())
        with self.assertRaisesRegex(NothingToRebuild, "changes in emails"):
            split_person(self.graph, self.db, ["A1", "A2"])
        self.assertEqual((self.graph.nodes, self.graph.relationships), before)
        self.assertEqual(list(self.db.persons.find()), prepared_before)

    def test_group_membership_is_restored_even_if_duplicate_survives_elsewhere(self):
        self.seed_pair()
        other = PreparedStore(self.db, "other")
        other.upsert_rows("persons", list(self.prepared.get_rows("persons", ["A2"])))
        self.prepared_fold()
        self.assertEqual(self.db.persons.find_one({"id": "A2"})["groups"], ["other"])
        split_person(self.graph, self.db, ["A1", "A2"])
        self.assertEqual(set(self.db.persons.find_one({"id": "A2"})["groups"]), {"other", self.prepared.group})
        self.republish()
        self.assertEqual(self.edges("A1"), {("AUTHORED", "W1")})

    def split_chain(self, first_to_restore):
        self.publish(
            person("A2", "Ivan Smirnov", works=["W1"], orcid="0000-1"),
            person("A3", "I. Smirnov", works=["W2"], orcid="0000-1"),
            publications=("W1", "W2", "W3", "W4", "W5"),
        )
        self.prepared_fold()
        self.prepared.upsert_models("persons", [
            person("A1", "Ivan Smirnov", works=["W3", "W4", "W5"], orcid="0000-1"),
        ])
        self.prepared_fold()
        self.assertEqual(set(self.graph.nodes[("Person", "A1")]["merged_ids"]), {"A2", "A3"})
        if first_to_restore == "A2":
            split_person(self.graph, self.db, ["A1", "A2"])
            split_person(self.graph, self.db, ["A2", "A3"])
        else:
            split_person(self.graph, self.db, ["A1", "A3"])
            split_person(self.graph, self.db, ["A1", "A2"])
        self.republish()
        self.assertEqual(self.edges("A1"), {("AUTHORED", "W3"), ("AUTHORED", "W4"), ("AUTHORED", "W5")})
        self.assertEqual(self.edges("A2"), {("AUTHORED", "W1")})
        self.assertEqual(self.edges("A3"), {("AUTHORED", "W2")})
        for identifier in ("A1", "A2", "A3"):
            self.assertEqual(self.graph.nodes[("Person", identifier)]["merged_ids"], [])

    def test_nested_chain_can_be_undone_parent_first(self):
        self.split_chain("A2")

    def test_nested_chain_can_be_undone_child_first(self):
        self.split_chain("A3")
