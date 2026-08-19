import unittest

import mongomock

from pauk.graph.jsonl_loader import load_prepared_rows
from pauk.graph.load import _drop_tombstoned
from pauk.graph.mutations import MutationError, UnknownEntity
from pauk.graph.overrides import (
    COLLECTION,
    active_overrides,
    apply_overrides,
    deactivate_override,
    deactivate_relationship_override,
    record_override,
    record_relationship_override,
    tombstoned_ids,
    tombstoned_relationships,
)

from .test_mutations import FakeGraph


class RecordTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]

    def test_an_edit_is_stored_under_a_key_of_its_own(self):
        stored = record_override(self.db, "Person", "A1", "set",
                                 {"name_ru": "Иванов И. И."}, actor="user:petrov")
        self.assertEqual(stored["_id"], "node:Person:A1")
        self.assertEqual(stored["fields"], {"name_ru": "Иванов И. И."})
        self.assertTrue(stored["active"])

    def test_editing_the_same_node_twice_keeps_both_fields(self):
        # Two people, two days, two different fields of one person: a
        # replace would silently drop the earlier edit.
        record_override(self.db, "Person", "A1", "set", {"name_ru": "Иванов И. И."})
        record_override(self.db, "Person", "A1", "set", {"email": "ivanov@itmo.ru"})
        (stored,) = active_overrides(self.db)
        self.assertEqual(stored["fields"],
                         {"name_ru": "Иванов И. И.", "email": "ivanov@itmo.ru"})

    def test_the_same_field_edited_again_takes_the_newer_value(self):
        record_override(self.db, "Person", "A1", "set", {"name_ru": "первый"})
        record_override(self.db, "Person", "A1", "set", {"name_ru": "второй"})
        (stored,) = active_overrides(self.db)
        self.assertEqual(stored["fields"], {"name_ru": "второй"})

    def test_the_automatic_value_recorded_is_the_original_one(self):
        # A second edit must not overwrite what the pipeline had with what
        # the previous manual edit left, or the conflict screen compares
        # an edit against an edit.
        record_override(self.db, "Person", "A1", "set", {"name_ru": "первый"},
                        auto_value={"name_ru": "Ivanov I."})
        record_override(self.db, "Person", "A1", "set", {"name_ru": "второй"},
                        auto_value={"name_ru": "первый"})
        (stored,) = active_overrides(self.db)
        self.assertEqual(stored["auto_value"], {"name_ru": "Ivanov I."})

    def test_the_first_edit_is_when_it_was_created(self):
        first = record_override(self.db, "Person", "A1", "set", {"name_ru": "первый"})
        second = record_override(self.db, "Person", "A1", "set", {"name_ru": "второй"})
        self.assertEqual(first["created_at"], second["created_at"])
        self.assertGreaterEqual(second["updated_at"], first["updated_at"])

    def test_a_field_the_loader_never_publishes_is_refused(self):
        with self.assertRaises(UnknownEntity):
            record_override(self.db, "Person", "A1", "set", {"salary": 100})

    def test_an_unknown_label_is_refused(self):
        with self.assertRaises(UnknownEntity):
            record_override(self.db, "Employee", "A1", "set", {"name_ru": "x"})

    def test_an_unknown_operation_is_refused(self):
        with self.assertRaises(UnknownEntity):
            record_override(self.db, "Person", "A1", "archive", {"name_ru": "x"})

    def test_a_set_that_sets_nothing_is_refused(self):
        with self.assertRaises(MutationError):
            record_override(self.db, "Person", "A1", "set", {})

    def test_a_deletion_needs_no_fields(self):
        stored = record_override(self.db, "Person", "A1", "delete")
        self.assertEqual(stored["op"], "delete")

    def test_undoing_keeps_the_record(self):
        record_override(self.db, "Person", "A1", "set", {"name_ru": "Иванов"})
        self.assertTrue(deactivate_override(self.db, "Person", "A1"))
        self.assertEqual(active_overrides(self.db), [])
        # Still on file: the panel has to show the edit existed.
        self.assertEqual(self.db[COLLECTION].count_documents({}), 1)


class TombstoneTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]

    def test_deleted_ids_are_reported_for_the_loader_to_skip(self):
        record_override(self.db, "Person", "A1", "delete")
        record_override(self.db, "Person", "A2", "set", {"name_ru": "Иванов"})
        self.assertEqual(tombstoned_ids(self.db, "Person"), {"A1"})

    def test_a_tombstone_is_scoped_to_its_label(self):
        record_override(self.db, "Person", "A1", "delete")
        self.assertEqual(tombstoned_ids(self.db, "Publication"), set())

    def test_an_undone_deletion_stops_tombstoning(self):
        record_override(self.db, "Person", "A1", "delete")
        deactivate_override(self.db, "Person", "A1")
        self.assertEqual(tombstoned_ids(self.db, "Person"), set())


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        self.graph = FakeGraph()
        self.graph.add("Person", "A1", name_en="Ivan Petrov", name_ru="Ivanov I.")

    def test_an_override_is_written_to_the_graph(self):
        record_override(self.db, "Person", "A1", "set", {"name_ru": "Иванов И. И."})
        result = apply_overrides(self.graph, self.db)
        self.assertEqual(self.graph.nodes[("Person", "A1")]["name_ru"], "Иванов И. И.")
        self.assertEqual(result["overrides_applied"], 1)

    def test_applying_twice_writes_only_once(self):
        # Reapplying happens after every publish; a second write would
        # stamp an audit entry for a change nobody made.
        record_override(self.db, "Person", "A1", "set", {"name_ru": "Иванов И. И."})
        apply_overrides(self.graph, self.db)
        writes = self.graph.calls.count("upsert_nodes_batch")
        result = apply_overrides(self.graph, self.db)
        self.assertEqual(self.graph.calls.count("upsert_nodes_batch"), writes)
        self.assertEqual((result["overrides_applied"], result["overrides_unchanged"]), (0, 1))

    def test_an_override_wins_back_after_the_pipeline_overwrites_it(self):
        record_override(self.db, "Person", "A1", "set", {"name_ru": "Иванов И. И."})
        apply_overrides(self.graph, self.db)
        # publish runs and puts the automatic value back
        self.graph.upsert_nodes_batch("Person", [("A1", {"name_ru": "Ivanov I."})])
        apply_overrides(self.graph, self.db)
        self.assertEqual(self.graph.nodes[("Person", "A1")]["name_ru"], "Иванов И. И.")

    def test_a_deletion_override_removes_the_node_again(self):
        record_override(self.db, "Person", "A1", "delete")
        apply_overrides(self.graph, self.db)
        self.assertNotIn(("Person", "A1"), self.graph.nodes)

    def test_a_deletion_whose_node_is_already_gone_is_quiet(self):
        record_override(self.db, "Person", "A1", "delete")
        apply_overrides(self.graph, self.db)
        result = apply_overrides(self.graph, self.db)
        self.assertEqual((result["overrides_applied"], result["overrides_missing"]), (0, 0))

    def test_an_edit_whose_node_vanished_is_reported_not_raised(self):
        record_override(self.db, "Person", "gone", "set", {"name_ru": "Иванов"})
        result = apply_overrides(self.graph, self.db)
        self.assertEqual(result["overrides_missing"], 1)

    def test_an_undone_override_is_not_applied(self):
        record_override(self.db, "Person", "A1", "set", {"name_ru": "Иванов И. И."})
        deactivate_override(self.db, "Person", "A1")
        apply_overrides(self.graph, self.db)
        self.assertEqual(self.graph.nodes[("Person", "A1")]["name_ru"], "Ivanov I.")


if __name__ == "__main__":
    unittest.main()


class TombstoneFilterTest(unittest.TestCase):
    """The loader must not even write a row whose node was deleted by hand."""

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]

    def test_a_deleted_person_is_dropped_before_loading(self):
        record_override(self.db, "Person", "A1", "delete")
        rows = {"persons.jsonl": [{"id": "A1"}, {"id": "A2"}],
                "publications.jsonl": [{"id": "W1"}]}
        kept = _drop_tombstoned(rows, self.db)
        self.assertEqual([row["id"] for row in kept["persons.jsonl"]], ["A2"])
        self.assertEqual(len(kept["publications.jsonl"]), 1)

    def test_rows_pass_through_untouched_without_tombstones(self):
        rows = {"persons.jsonl": [{"id": "A1"}]}
        self.assertEqual(_drop_tombstoned(rows, self.db), rows)

    def test_a_file_with_no_label_of_its_own_is_left_alone(self):
        # repo_links.jsonl carries relationships, not nodes.
        record_override(self.db, "Person", "A1", "delete")
        rows = {"repo_links.jsonl": [{"publication_id": "W1"}]}
        self.assertEqual(_drop_tombstoned(rows, self.db), rows)


class RelationshipOverrideTest(unittest.TestCase):
    """An edge removed by hand is rebuilt by MERGE from the same prepared row."""

    TRIPLE = ("Person", "AUTHORED", "Publication", "A1", "W1")

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        self.graph = FakeGraph()
        self.graph.add("Person", "A1", name_en="Ivan Petrov")
        self.graph.add("Publication", "W1", title="paper")

    def test_an_unlinked_edge_is_remembered(self):
        stored = record_relationship_override(self.db, *self.TRIPLE, actor="user:petrov")
        self.assertEqual(stored["_id"], "rel:Person:AUTHORED:Publication:A1:W1")
        self.assertEqual(stored["kind"], "rel")

    def test_the_loader_is_told_which_edges_to_skip(self):
        record_relationship_override(self.db, *self.TRIPLE)
        self.assertEqual(tombstoned_relationships(self.db), {self.TRIPLE})

    def test_a_publish_does_not_rebuild_an_unlinked_edge(self):
        record_relationship_override(self.db, *self.TRIPLE)
        rows = {"persons.jsonl": [
            {"id": "A1", "is_itmo": True, "authored": [{"publication_id": "W1", "position": 1}]}]}
        load_prepared_rows(self.graph, rows, tombstoned_relationships(self.db))
        self.assertNotIn(("Person", "AUTHORED", "Publication", "A1", "W1"),
                         self.graph.relationships)

    def test_without_the_override_the_edge_comes_back(self):
        rows = {"persons.jsonl": [
            {"id": "A1", "is_itmo": True, "authored": [{"publication_id": "W1", "position": 1}]}]}
        load_prepared_rows(self.graph, rows, tombstoned_relationships(self.db))
        self.assertIn(("Person", "AUTHORED", "Publication", "A1", "W1"),
                      self.graph.relationships)

    def test_only_the_named_edge_is_skipped(self):
        record_relationship_override(self.db, *self.TRIPLE)
        self.graph.add("Publication", "W2", title="another")
        rows = {"persons.jsonl": [{"id": "A1", "is_itmo": True, "authored": [
            {"publication_id": "W1", "position": 1}, {"publication_id": "W2", "position": 2}]}]}
        load_prepared_rows(self.graph, rows, tombstoned_relationships(self.db))
        self.assertIn(("Person", "AUTHORED", "Publication", "A1", "W2"),
                      self.graph.relationships)

    def test_restoring_lets_the_next_publish_rebuild_it(self):
        record_relationship_override(self.db, *self.TRIPLE)
        self.assertTrue(deactivate_relationship_override(self.db, *self.TRIPLE))
        self.assertEqual(tombstoned_relationships(self.db), set())

    def test_a_triple_the_graph_does_not_have_is_refused(self):
        with self.assertRaises(UnknownEntity):
            record_relationship_override(self.db, "Person", "AUTHORED", "Department", "A1", "D1")

    def test_only_deletion_is_recorded_for_edges(self):
        # An edge added by hand already survives: the loader never removes
        # edges it does not know about.
        with self.assertRaises(UnknownEntity):
            record_relationship_override(self.db, *self.TRIPLE, op="set")

    def test_applying_removes_an_edge_recreated_behind_our_back(self):
        record_relationship_override(self.db, *self.TRIPLE)
        self.graph.upsert_relationships_batch("Person", "Publication", "AUTHORED",
                                              [("A1", "W1", {})])
        apply_overrides(self.graph, self.db)
        self.assertNotIn(("Person", "AUTHORED", "Publication", "A1", "W1"),
                         self.graph.relationships)

    def test_a_row_the_registry_no_longer_knows_does_not_break_a_publish(self):
        # apply_overrides runs inside publish; a relationship type dropped
        # in a refactor, or a hand-edited document, must not take a whole
        # group's publish down with it.
        self.db[COLLECTION].insert_one({
            "_id": "rel:Person:WAS_ADVISOR:Person:A1:A2", "kind": "rel", "active": True,
            "op": "delete", "src_label": "Person", "rel_type": "WAS_ADVISOR",
            "tgt_label": "Person", "src_id": "A1", "target_id": "A2",
            "actor": "x", "note": "",
        })
        result = apply_overrides(self.graph, self.db)
        self.assertEqual(result["overrides_applied"], 0)
        self.assertEqual(result["overrides_missing"], 1)

    def test_an_edge_whose_target_is_matched_by_url_is_skipped_too(self):
        # MENTIONS_LINK finds its Repository by url, not by id — the
        # tombstone has to be keyed the same way the loader looks it up.
        self.graph.add("Publication", "W2", title="paper with code")
        self.graph.nodes[("Repository", "github_org_repo")] = {
            "id": "github_org_repo", "url": "https://github.com/org/repo"}
        record_relationship_override(self.db, "Publication", "MENTIONS_LINK", "Repository",
                                     "W2", "https://github.com/org/repo")
        rows = {
            "repositories.jsonl": [{"id": "github_org_repo", "name": "repo",
                                    "url": "https://github.com/org/repo"}],
            "repo_links.jsonl": [{"publication_id": "W2", "links": [
                {"url": "https://github.com/org/repo",
                 "occurrences": [{"context": "see code"}]}]}],
        }
        load_prepared_rows(self.graph, rows, tombstoned_relationships(self.db))
        self.assertEqual([k for k in self.graph.relationships if k[1] == "MENTIONS_LINK"], [])

    def test_every_entity_the_publish_loads_can_be_tombstoned(self):
        # A new prepared entity (organizations, when department matching
        # landed) must not lose its tombstones because a second list was
        # never updated: deleting such a node by hand would then be undone
        # by the very next publish.
        from pauk.graph.load import ENTITY_FILES, FILE_LABELS
        node_files = {name for name in ENTITY_FILES.values() if name != "repo_links.jsonl"}
        self.assertEqual(node_files - set(FILE_LABELS), set())
