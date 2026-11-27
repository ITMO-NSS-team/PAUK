"""CSV repository postconditions against an isolated Neo4j container."""

import csv
import json
import os
import tempfile
import unittest
from pathlib import Path

from pauk.graph.client import Neo4jClient
from pauk.graph.csv_loader import load_csv_dir
from pauk.graph.schema import create_constraints

try:
    from testcontainers.community.neo4j import Neo4jContainer
except ImportError:
    Neo4jContainer = None


class CsvRepositoryUpdatesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if Neo4jContainer is None:
            raise unittest.SkipTest("testcontainers is not installed")
        cls.container = Neo4jContainer(image="neo4j:5-community", password="csv-integration-password")
        try:
            cls.container.start()
        except Exception as exc:
            if os.environ.get("CI"):
                raise
            raise unittest.SkipTest(f"could not start an isolated Neo4j container: {exc}") from exc
        cls.addClassCleanup(cls.container.stop)
        cls.client = Neo4jClient(
            cls.container.get_connection_url(), cls.container.username, "csv-integration-password",
        )
        cls.addClassCleanup(cls.client.close)
        create_constraints(cls.client)

    def setUp(self):
        with self.client.driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n").consume()

    def test_partial_update_preserves_name_other_fields_and_relationships(self):
        self.client.upsert_nodes_batch("Repository:Reviewed", [("R1", {
            "name": " Kept ", "url": "https://github.com/org/repo", "description": "Before",
        })])
        self.client.upsert_nodes_batch("Publication", [("W1", {"title": "Paper"})])
        self.client.upsert_relationships_batch("Repository", "Publication", "IMPLEMENTS", [("R1", "W1", {})])
        before = self.client.fetch_node_properties("Repository", "R1")
        self.client.upsert_csv_node("Reviewed:Repository", "R1", {
            "id": "R2", "created_at": "bad", "updated_at": "bad", "description": "After",
        })
        after = self.client.fetch_node_properties("Repository", "R1")
        for field in ("id", "name", "url", "created_at"):
            self.assertEqual(after[field], before[field])
        self.assertEqual(after["description"], "After")
        self.assertNotIn("__pauk_edit_lock", after)
        self.assertIsNone(self.client.fetch_node_properties("Repository", "R2"))
        self.assertEqual(len(self.client.fetch_node_relationships("Repository", "R1")), 1)

    def test_omitted_name_cannot_create_a_node(self):
        with self.assertRaisesRegex(ValueError, "Repository.name"):
            self.client.upsert_csv_node("Repository", "R1", {"description": "After"})
        self.assertIsNone(self.client.fetch_node_properties("Repository", "R1"))

    def test_omitted_name_does_not_borrow_a_name_from_a_different_label_match(self):
        self.client.upsert_nodes_batch("Repository", [("R1", {"name": "Existing"})])
        with self.assertRaisesRegex(ValueError, "Repository.name"):
            self.client.upsert_csv_node("Repository:Reviewed", "R1", {"description": "After"})
        self.assertIsNone(self.client.fetch_node_properties("Repository:Reviewed", "R1"))
        self.assertEqual(self.client.fetch_node_properties("Repository", "R1")["name"], "Existing")

    def test_invalid_explicit_names_preserve_the_entire_existing_node(self):
        self.client.upsert_nodes_batch("Repository", [("R1", {"name": "Existing", "description": "Before"})])
        before = self.client.fetch_node_properties("Repository", "R1")
        for name in (None, "", " \t", 0):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "Repository.name"):
                self.client.upsert_csv_node("Repository", "R1", {"name": name, "description": "After"})
            self.assertEqual(self.client.fetch_node_properties("Repository", "R1"), before)

    def test_legacy_missing_name_rejects_partial_update_then_accepts_repair(self):
        self.client.upsert_nodes_batch("Repository", [("R1", {"description": "Before"})])
        before = self.client.fetch_node_properties("Repository", "R1")
        with self.assertRaisesRegex(ValueError, "Repository.name"):
            self.client.upsert_csv_node("Repository", "R1", {"description": "After"})
        self.assertEqual(self.client.fetch_node_properties("Repository", "R1"), before)
        self.client.upsert_csv_node("Repository", "R1", {"name": "Repaired"})
        self.client.upsert_csv_node("Repository", "R1", {"description": "After"})
        after = self.client.fetch_node_properties("Repository", "R1")
        self.assertEqual(after["name"], "Repaired")
        self.assertEqual(after["description"], "After")

    def test_create_then_partial_update_uses_the_created_name(self):
        self.client.upsert_csv_node("Repository", "R1", {"name": "Created"})
        self.client.upsert_csv_node("Repository", "R1", {"description": "After"})
        after = self.client.fetch_node_properties("Repository", "R1")
        self.assertEqual(after["name"], "Created")
        self.assertEqual(after["description"], "After")

    def test_secondary_label_name_update_obeys_repository_postcondition(self):
        self.client.upsert_nodes_batch("Repository:Reviewed", [("R1", {"name": "Keep", "description": "Before"})])
        before = self.client.fetch_node_properties("Repository", "R1")
        for name in (None, "", 42):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "Repository.name"):
                self.client.upsert_csv_node("Reviewed", "R1", {"name": name, "description": "After"})
            self.assertEqual(self.client.fetch_node_properties("Repository", "R1"), before)
        self.client.upsert_csv_node("Reviewed", "R1", {"name": "New"})
        self.assertEqual(self.client.fetch_node_properties("Repository", "R1")["name"], "New")

    def test_ambiguous_secondary_label_match_rolls_back_both_nodes(self):
        self.client.upsert_nodes_batch("Repository:Reviewed", [("same", {"name": "Repository"})])
        self.client.upsert_nodes_batch("Person:Reviewed", [("same", {"name": "Person"})])
        before = [self.client.fetch_node_properties(label, "same") for label in ("Repository", "Person")]
        with self.assertRaisesRegex(ValueError, "more than one node"):
            self.client.upsert_csv_node("Reviewed", "same", {"name": "Wrong"})
        self.assertEqual([self.client.fetch_node_properties(label, "same") for label in ("Repository", "Person")], before)

    def test_mixed_directory_import_continues_and_counts_missing_relationships(self):
        self.client.upsert_nodes_batch("Repository", [("R-existing", {
            "name": "Existing", "description": "Before", "url": "https://github.com/org/existing",
        })])
        before = self.client.fetch_node_properties("Repository", "R-existing")
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            node_fields = ["id", "labels", "properties"]
            edge_fields = ["start_id", "end_id", "src_label", "tgt_label", "type", "properties"]

            def write(filename, fields, rows):
                with (directory / filename).open("w", encoding="utf-8", newline="") as stream:
                    writer = csv.writer(stream)
                    writer.writerow(fields)
                    writer.writerows(rows)

            write("a_nodes.csv", node_fields, [
                ("R-new", "Repository", json.dumps({"name": "New"})),
                ("R-bad-json", "Repository", "{broken"),
                ("R-missing", "Repository", json.dumps({"description": "No name"})),
                ("R-existing", "Repository", json.dumps({"description": "After"})),
                ("R-existing", "Repository", json.dumps({"name": None, "description": "Rejected"})),
            ])
            write("z_nodes.csv", node_fields, [("W1", "Publication", json.dumps({"title": "Paper"}))])
            write("a_rels.csv", edge_fields, [
                ("R-new", "W1", "Repository", "Publication", "IMPLEMENTS", "{}"),
                ("R-missing", "W1", "Repository", "Publication", "IMPLEMENTS", "{}"),
                ("R-new", "W1", "Repository", "Publication", "IMPLEMENTS", "{broken"),
            ])
            write("z_rels.csv", edge_fields, [
                ("R-existing", "W1", "Repository", "Publication", "IMPLEMENTS", "{}"),
            ])
            report = load_csv_dir(self.client, directory)

        self.assertEqual((report.nodes_loaded, report.nodes_skipped), (3, 3))
        self.assertEqual((report.relationships_loaded, report.relationships_skipped), (2, 2))
        self.assertEqual(len(report.issues), 5)
        after = self.client.fetch_node_properties("Repository", "R-existing")
        self.assertEqual(after["description"], "After")
        for name in ("name", "url", "created_at"):
            self.assertEqual(after[name], before[name])
        self.assertIsNone(self.client.fetch_node_properties("Repository", "R-bad-json"))
        self.assertIsNone(self.client.fetch_node_properties("Repository", "R-missing"))
        self.assertEqual(len(self.client.fetch_node_relationships("Publication", "W1")), 2)

    def test_secondary_label_import_checks_existing_repository_name(self):
        self.client.upsert_nodes_batch("Repository:Reviewed", [("R1", {"description": "Before"})])
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with (directory / "a_nodes.csv").open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["id", "labels", "properties"])
                writer.writerows([
                    ("R1", "Reviewed", json.dumps({"description": "Rejected"})),
                    ("R1", "Reviewed", json.dumps({"name": "Repaired"})),
                    ("R1", "Reviewed", json.dumps({"description": "After"})),
                ])
            report = load_csv_dir(self.client, directory)
        self.assertEqual((report.nodes_loaded, report.nodes_skipped), (2, 1))
        self.assertEqual(report.issues[0]["line"], 2)
        self.assertEqual(report.issues[0]["id"], "R1")
        stored = self.client.fetch_node_properties("Repository", "R1")
        self.assertEqual((stored["name"], stored["description"]), ("Repaired", "After"))

    def test_edge_import_reports_ambiguous_missing_and_valid_rows_separately(self):
        self.client.upsert_nodes_batch("Person:Reviewed", [("shared", {})])
        self.client.upsert_nodes_batch("Organization:Reviewed", [("shared", {})])
        self.client.upsert_nodes_batch("Person", [("P1", {})])
        self.client.upsert_nodes_batch("Publication", [("W1", {})])
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with (directory / "a_rels.csv").open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["start_id", "end_id", "src_label", "tgt_label", "type", "properties"])
                writer.writerows([
                    ("shared", "W1", "Reviewed", "Publication", "AUTHORED", "{}"),
                    ("missing", "W1", "Reviewed", "Publication", "AUTHORED", "{}"),
                    ("P1", "W1", "Person", "Publication", "AUTHORED", "{}"),
                ])
            report = load_csv_dir(self.client, directory)
        self.assertEqual((report.relationships_loaded, report.relationships_skipped), (1, 2))
        self.assertEqual([(row["line"], row["id"]) for row in report.issues],
                         [(2, "shared->W1"), (3, "missing->W1")])
        self.assertEqual(len(self.client.fetch_node_relationships("Publication", "W1")), 1)

    def test_constraint_conflict_does_not_block_next_csv_file(self):
        self.client.upsert_nodes_batch("Repository", [("R1", {"name": "Before"})])
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for filename, rows in (
                ("a_nodes.csv", [("R1", "Repository:Reviewed", json.dumps({"name": "Conflict"}))]),
                ("z_nodes.csv", [("R2", "Repository", json.dumps({"name": "Valid"}))]),
            ):
                with (directory / filename).open("w", encoding="utf-8", newline="") as stream:
                    writer = csv.writer(stream)
                    writer.writerow(["id", "labels", "properties"])
                    writer.writerows(rows)
            report = load_csv_dir(self.client, directory)
        self.assertEqual((report.nodes_loaded, report.nodes_skipped), (1, 1))
        self.assertEqual((report.issues[0]["line"], report.issues[0]["id"]), (2, "R1"))
        self.assertEqual(self.client.fetch_node_properties("Repository", "R1")["name"], "Before")
        self.assertEqual(self.client.fetch_node_properties("Repository", "R2")["name"], "Valid")


if __name__ == "__main__":
    unittest.main()
