import argparse
import unittest
from unittest.mock import patch

import mongomock

from pauk.admin import cli as admin_cli
from pauk.admin import decisions
from pauk.graph.audit import _actor_var, _source_var
from pauk.graph.mutations import NotFound
from pauk.graph.overrides import (
    COLLECTION,
    active_overrides,
    tombstoned_ids,
    tombstoned_relationships,
)
from pauk.settings import Settings
from tests.unit.test_mutations import FakeGraph


def parse(*argv):
    parser = argparse.ArgumentParser()
    admin_cli.add_parser(parser.add_subparsers(dest="command", required=True))
    return parser.parse_args(["admin", *argv])


class ParseValueTest(unittest.TestCase):
    """`--set field=value` has to survive the shell without types."""

    def test_a_number_is_stored_as_a_number(self):
        self.assertEqual(admin_cli._parse_value("10"), 10)

    def test_a_name_is_stored_as_text(self):
        self.assertEqual(admin_cli._parse_value("Иванов И. И."), "Иванов И. И.")

    def test_a_list_can_be_given(self):
        self.assertEqual(admin_cli._parse_value('["a", "b"]'), ["a", "b"])

    def test_booleans_and_null(self):
        self.assertIs(admin_cli._parse_value("true"), True)
        self.assertIsNone(admin_cli._parse_value("null"))

    def test_a_url_stays_a_string(self):
        self.assertEqual(admin_cli._parse_value("https://github.com/x/y"),
                         "https://github.com/x/y")

    def test_assignments_without_an_equals_sign_are_refused(self):
        with self.assertRaises(SystemExit):
            admin_cli._parse_assignments(["justafield"])

    def test_a_value_containing_an_equals_sign_survives(self):
        self.assertEqual(
            admin_cli._parse_assignments(["url=https://x.org/?a=1"]),
            {"url": "https://x.org/?a=1"})


class RunTest(unittest.TestCase):
    def setUp(self):
        self.graph = FakeGraph()
        self.graph.add("Person", "A1", name_en="Ivan Petrov")
        self.config = Settings()
        patcher = patch("pauk.admin.cli.audited_client", return_value=self.graph)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.graph.close = lambda: None

    def test_setting_a_field_reaches_the_graph(self):
        admin_cli.run(parse("node", "set", "Person", "A1", "--set", "name_ru=Иванов"),
                      self.config, None)
        self.assertEqual(self.graph.nodes[("Person", "A1")]["name_ru"], "Иванов")

    def test_an_unknown_field_stops_with_a_message_not_a_traceback(self):
        # The person at the keyboard should read what went wrong.
        with self.assertRaises(SystemExit) as caught:
            admin_cli.run(parse("node", "set", "Person", "A1", "--set", "salary=100"),
                          self.config, None)
        self.assertIn("unknown field", str(caught.exception))

    def test_deleting_a_connected_node_explains_the_refusal(self):
        self.graph.add("Publication", "W1", title="paper")
        admin_cli.run(parse("rel", "add", "Person", "AUTHORED", "Publication", "A1", "W1"),
                      self.config, None)
        with self.assertRaises(SystemExit) as caught:
            admin_cli.run(parse("node", "delete", "Person", "A1"), self.config, None)
        self.assertIn("cascade", str(caught.exception))

    def test_the_actor_defaults_to_the_os_user_and_reaches_the_audit(self):
        # Read inside the write itself: this is exactly what the audit
        # wrapper reads when it stamps an entry.
        seen = {}
        original = self.graph.upsert_nodes_batch

        def spy(labels, nodes):
            seen["actor"] = _actor_var.get()
            seen["source"] = _source_var.get()
            return original(labels, nodes)

        self.graph.upsert_nodes_batch = spy
        with patch("getpass.getuser", return_value="petrov"):
            admin_cli.run(parse("node", "set", "Person", "A1", "--set", "name_ru=Иванов"),
                          self.config, None)
        self.assertEqual((seen["actor"], seen["source"]), ("user:petrov", "admin-cli"))

    def test_an_explicit_actor_wins(self):
        args = parse("--actor", "user:ivanova", "node", "set", "Person", "A1",
                     "--set", "name_ru=Иванова")
        self.assertEqual(args.actor, "user:ivanova")

    def test_merging_asks_before_doing_something_irreversible(self):
        self.graph.add("Person", "A2", name_en="I. Petrov")
        with patch("builtins.input", return_value="n"), self.assertRaises(SystemExit):
            admin_cli.run(parse("merge", "Person", "A2", "A1"), self.config, None)
        self.assertIn(("Person", "A2"), self.graph.nodes)

    def test_merging_proceeds_when_confirmed(self):
        self.graph.add("Person", "A2", name_en="I. Petrov")
        with patch("builtins.input", return_value="y"):
            admin_cli.run(parse("merge", "Person", "A2", "A1"), self.config, None)
        self.assertNotIn(("Person", "A2"), self.graph.nodes)
        self.assertEqual(self.graph.nodes[("Person", "A1")]["merged_ids"], ["A2"])

    def test_yes_skips_the_prompt(self):
        self.graph.add("Person", "A2", name_en="I. Petrov")
        with patch("builtins.input", side_effect=AssertionError("must not ask")):
            admin_cli.run(parse("merge", "Person", "A2", "A1", "--yes"), self.config, None)
        self.assertNotIn(("Person", "A2"), self.graph.nodes)

    def test_an_edit_leaves_an_override_so_a_publish_cannot_undo_it(self):
        db = mongomock.MongoClient()["pauk_test"]
        admin_cli.run(parse("node", "set", "Person", "A1", "--set", "name_ru=Иванов И. И.",
                            "--note", "сверено с приказом"), self.config, db)
        (stored,) = active_overrides(db)
        self.assertEqual(stored["fields"], {"name_ru": "Иванов И. И."})
        self.assertEqual(stored["note"], "сверено с приказом")
        # What the pipeline had, for the conflict screen later.
        self.assertEqual(stored["auto_value"], {"name_ru": None})

    def test_once_edits_the_graph_without_recording_a_decision(self):
        db = mongomock.MongoClient()["pauk_test"]
        admin_cli.run(parse("node", "set", "Person", "A1", "--set", "name_ru=разово", "--once"),
                      self.config, db)
        self.assertEqual(self.graph.nodes[("Person", "A1")]["name_ru"], "разово")
        self.assertEqual(active_overrides(db), [])

    def test_deleting_leaves_a_tombstone(self):
        db = mongomock.MongoClient()["pauk_test"]
        admin_cli.run(parse("node", "delete", "Person", "A1"), self.config, db)
        self.assertEqual(tombstoned_ids(db, "Person"), {"A1"})

    def test_undoing_an_override_reports_when_there_is_none(self):
        db = mongomock.MongoClient()["pauk_test"]
        with self.assertRaises(SystemExit):
            admin_cli.run(parse("overrides", "undo", "Person", "A1"), self.config, db)

    def test_overrides_can_be_reapplied_from_the_shell(self):
        db = mongomock.MongoClient()["pauk_test"]
        admin_cli.run(parse("node", "set", "Person", "A1", "--set", "name_ru=Иванов"),
                      self.config, db)
        self.graph.upsert_nodes_batch("Person", [("A1", {"name_ru": "Ivanov I."})])  # publish
        admin_cli.run(parse("overrides", "apply"), self.config, db)
        self.assertEqual(self.graph.nodes[("Person", "A1")]["name_ru"], "Иванов")

    def test_a_refused_edit_leaves_no_decision_behind(self):
        # Recording the decision before the write would apply it on the next
        # publish — quietly making the change the person was just told was
        # rejected.
        db = mongomock.MongoClient()["pauk_test"]
        stale = self.graph.nodes[("Person", "A1")]["updated_at"]
        self.graph.upsert_nodes_batch("Person", [("A1", {"name_ru": "чужая правка"})])
        with self.assertRaises(SystemExit):
            admin_cli.run(parse("node", "set", "Person", "A1", "--set", "name_ru=моя правка",
                                "--expect-updated-at", stale), self.config, db)
        self.assertEqual(active_overrides(db), [])
        self.assertEqual(self.graph.nodes[("Person", "A1")]["name_ru"], "чужая правка")

    def test_a_refused_deletion_leaves_no_tombstone(self):
        db = mongomock.MongoClient()["pauk_test"]
        self.graph.add("Publication", "W1", title="paper")
        admin_cli.run(parse("rel", "add", "Person", "AUTHORED", "Publication", "A1", "W1"),
                      self.config, db)
        with self.assertRaises(SystemExit):
            admin_cli.run(parse("node", "delete", "Person", "A1"), self.config, db)
        self.assertEqual(tombstoned_ids(db, "Person"), set())
        self.assertIn(("Person", "A1"), self.graph.nodes)

    def test_schema_needs_no_database_at_all(self):
        # It only prints the whitelists; requiring Mongo would make the
        # one command you reach for while setting things up unusable.
        with patch("pauk.admin.cli.audited_client",
                   side_effect=AssertionError("must not connect")):
            admin_cli.run(parse("schema"), self.config, None)


if __name__ == "__main__":
    unittest.main()


class RelationshipOverrideCliTest(unittest.TestCase):
    def setUp(self):
        self.graph = FakeGraph()
        self.graph.add("Person", "A1", name_en="Ivan Petrov")
        self.graph.add("Publication", "W1", title="paper")
        self.graph.close = lambda: None
        self.config = Settings()
        patcher = patch("pauk.admin.cli.audited_client", return_value=self.graph)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.db = mongomock.MongoClient()["pauk_test"]
        admin_cli.run(parse("rel", "add", "Person", "AUTHORED", "Publication", "A1", "W1"),
                      self.config, self.db)

    def test_unlinking_is_remembered_so_a_publish_cannot_restore_it(self):
        admin_cli.run(parse("rel", "delete", "Person", "AUTHORED", "Publication", "A1", "W1",
                            "--note", "не его статья"), self.config, self.db)
        self.assertEqual(tombstoned_relationships(self.db),
                         {("Person", "AUTHORED", "Publication", "A1", "W1")})

    def test_linking_records_nothing(self):
        # The loader only creates edges, so a hand-made link needs no help.
        self.assertEqual(active_overrides(self.db), [])

    def test_once_unlinks_without_remembering(self):
        admin_cli.run(parse("rel", "delete", "Person", "AUTHORED", "Publication", "A1", "W1",
                            "--once"), self.config, self.db)
        self.assertEqual(tombstoned_relationships(self.db), set())

    def test_restoring_a_link_from_the_shell(self):
        admin_cli.run(parse("rel", "delete", "Person", "AUTHORED", "Publication", "A1", "W1"),
                      self.config, self.db)
        admin_cli.run(parse("overrides", "undo-rel", "Person", "AUTHORED", "Publication",
                            "A1", "W1"), self.config, self.db)
        self.assertEqual(tombstoned_relationships(self.db), set())

    def test_restoring_something_never_unlinked_says_so(self):
        with self.assertRaises(SystemExit):
            admin_cli.run(parse("overrides", "undo-rel", "Person", "AUTHORED", "Publication",
                                "A1", "W1"), self.config, self.db)


class PrintingCommandsTest(unittest.TestCase):
    """Commands that mostly print — still worth a run, they touch the graph."""

    def setUp(self):
        self.graph = FakeGraph()
        self.graph.add("Person", "A1", name_en="Ivan Petrov")
        self.graph.close = lambda: None
        self.config = Settings()
        patcher = patch("pauk.admin.cli.audited_client", return_value=self.graph)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.db = mongomock.MongoClient()["pauk_test"]

    def test_show_prints_the_node(self):
        with patch("builtins.print") as printed:
            admin_cli.run(parse("node", "show", "Person", "A1"), self.config, self.db)
        self.assertIn("Ivan Petrov", printed.call_args[0][0])

    def test_show_of_a_missing_node_stops_with_a_message(self):
        with self.assertRaises(SystemExit) as caught:
            admin_cli.run(parse("node", "show", "Person", "nobody"), self.config, self.db)
        self.assertIn("does not exist", str(caught.exception))

    def test_create_adds_a_node_the_pipeline_does_not_know(self):
        admin_cli.run(parse("node", "create", "Department", "D1", "--set", "name_en=New lab"),
                      self.config, self.db)
        self.assertEqual(self.graph.nodes[("Department", "D1")]["name_en"], "New lab")

    def test_creating_over_an_existing_node_is_refused(self):
        with self.assertRaises(SystemExit) as caught:
            admin_cli.run(parse("node", "create", "Person", "A1", "--set", "name_en=Someone"),
                          self.config, self.db)
        self.assertIn("already exists", str(caught.exception))

    def test_listing_shows_both_kinds_of_decision(self):
        self.graph.add("Publication", "W1", title="paper")
        admin_cli.run(parse("node", "set", "Person", "A1", "--set", "name_ru=Иванов"),
                      self.config, self.db)
        admin_cli.run(parse("rel", "add", "Person", "AUTHORED", "Publication", "A1", "W1"),
                      self.config, self.db)
        admin_cli.run(parse("rel", "delete", "Person", "AUTHORED", "Publication", "A1", "W1"),
                      self.config, self.db)
        with patch("builtins.print") as printed:
            admin_cli.run(parse("overrides", "list"), self.config, self.db)
        printed_text = " ".join(str(call[0][0]) for call in printed.call_args_list)
        self.assertIn("node:Person:A1", printed_text)
        self.assertIn("unlink", printed_text)

    def test_listing_says_so_when_there_is_nothing(self):
        with patch("builtins.print") as printed:
            admin_cli.run(parse("overrides", "list"), self.config, self.db)
        self.assertIn("no active overrides", printed.call_args[0][0])

    def test_overrides_without_a_database_stops_clearly(self):
        with self.assertRaises(SystemExit) as caught:
            admin_cli.run(parse("overrides", "list"), self.config, None)
        self.assertIn("MongoDB", str(caught.exception))


class DeleteSnapshotTest(unittest.TestCase):
    """`pauk admin node delete` has to record what it removed.

    The panel took a snapshot and the command did not, so a record deleted
    from the terminal could only be restored from the feed — which keeps
    history rather than state, and summarises a bulk operation without
    listing a single field.
    """

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        self.graph = FakeGraph()
        self.graph.add("Person", "A1", name_ru="Иван Петров", name_en="Ivan Petrov")
        self.args = argparse.Namespace(label="Person", id="A1", cascade=False,
                                       once=False, note="")

    def test_the_decision_carries_the_fields(self):
        admin_cli._delete(self.args, self.graph, self.db, "user:roman")
        row = self.db[COLLECTION].find_one({"op": "delete"})
        self.assertEqual(row["snapshot"], {"name_ru": "Иван Петров", "name_en": "Ivan Petrov"})

    def test_the_record_can_be_restored_from_it(self):
        admin_cli._delete(self.args, self.graph, self.db, "user:roman")
        self.assertEqual(decisions.deleted_fields(self.db, "Person", "A1"),
                         {"name_ru": "Иван Петров", "name_en": "Ivan Petrov"})

    def test_nothing_is_recorded_with_once(self):
        self.args.once = True
        admin_cli._delete(self.args, self.graph, self.db, "user:roman")
        self.assertIsNone(self.db[COLLECTION].find_one({"op": "delete"}))

    def test_deleting_what_is_not_there_is_refused_before_the_decision(self):
        self.args.id = "nobody"
        with self.assertRaises(NotFound):
            admin_cli._delete(self.args, self.graph, self.db, "user:roman")
        self.assertIsNone(self.db[COLLECTION].find_one({"op": "delete"}))
