"""Mixed-file CSV workflows, with a stateful graph and database type boundaries."""

import csv
import json
import math
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock, Mock

from neo4j.exceptions import ResultNotSingleError

from pauk.graph.client import Neo4jClient
from pauk.graph.csv_loader import load_csv_dir, load_nodes_from_csv
from pauk.graph.validation import RepositoryNameError
from tests.unit.test_issue286_csv_names import MemoryGraph


class StrictPropertyGraph(MemoryGraph):
    """Model Neo4j's property limits instead of accepting every Python object."""

    @staticmethod
    def validate_properties(properties):
        for value in properties.values():
            if isinstance(value, dict):
                raise RuntimeError("Neo4j cannot store a map as a node property")
            if isinstance(value, int) and not -(2**63) <= value < 2**63:
                raise OverflowError("Neo4j integer is outside the signed 64-bit range")
            if isinstance(value, float) and not math.isfinite(value):
                raise RuntimeError("Non-finite numbers are not valid interchange JSON")
            if isinstance(value, list):
                if any(item is None or isinstance(item, (list, dict)) for item in value):
                    raise RuntimeError("Neo4j cannot store nested or null property-array elements")
                kinds = {type(item) for item in value}
                if len(kinds) > 1 and not kinds <= {int, float}:
                    raise RuntimeError("Neo4j property arrays must have homogeneous element types")

    def upsert_nodes_batch(self, labels, rows):
        for _, properties in rows:
            self.validate_properties(properties)
        return super().upsert_nodes_batch(labels, rows)

    def upsert_relationships_batch(self, src_label, tgt_label, rel_type, rows):
        for _, _, properties in rows:
            self.validate_properties(properties)
        return super().upsert_relationships_batch(src_label, tgt_label, rel_type, rows)


class DriverRows:
    """Keep real driver cardinality semantics that a plain Mock would hide."""

    def __init__(self, rows):
        self.rows = rows

    def __iter__(self):
        return iter(self.rows)

    def fetch(self, count):
        return self.rows[:count]

    def single(self, strict=False):
        if strict and len(self.rows) != 1:
            raise ResultNotSingleError(self, "result is not a single record")
        return self.rows[0] if self.rows else None


class CsvImportScenariosTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.graph = StrictPropertyGraph()

    def write(self, filename, header, rows):
        path = self.directory / filename
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(header)
            for *columns, properties in rows:
                writer.writerow([*columns, json.dumps(properties)])
        return path

    def nodes(self, filename, rows):
        return self.write(filename, ["id", "labels", "properties"], rows)

    def edges(self, filename, rows):
        return self.write(filename, ["start_id", "end_id", "src_label", "tgt_label", "type", "properties"], rows)

    def test_create_repair_and_retry_preserve_existing_fields_and_edges(self):
        self.graph.upsert_nodes_batch("Repository:Reviewed", [("R", {
            "name": "Kept", "description": "Old", "url": "https://github.com/org/repo",
        })])
        self.graph.upsert_nodes_batch("Publication", [("W", {"title": "Paper"})])
        self.graph.upsert_relationships_batch("Repository", "Publication", "IMPLEMENTS", [
            ("R", "W", {"evidence": "Existing"}),
        ])
        before_edge = deepcopy(self.graph.edges)
        self.nodes("a_nodes.csv", [
            ("R", "Reviewed:Repository", {"description": "Updated"}),
            ("R", "Repository", {"name": None, "url": "Must not replace"}),
            ("new", "Repository", {"description": "Must not create"}),
            ("new", "Repository", {"name": "Created"}),
        ])
        self.nodes("z_nodes.csv", [("new", "Repository", {"description": "Last"})])
        self.edges("links_rels.csv", [
            ("new", "W", "Repository", "Publication", "IMPLEMENTS", {"evidence": "New"}),
            ("missing", "W", "Repository", "Publication", "IMPLEMENTS", {}),
        ])
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                report = load_csv_dir(self.graph, self.directory)
                self.assertEqual(report.nodes_skipped, 2 if attempt == 0 else 1)
                self.assertEqual(report.relationships_loaded, 1)
                self.assertEqual(report.relationships_skipped, 1)
                self.assertEqual(self.graph.node("R"), {
                    "id": "R", "name": "Kept", "description": "Updated", "url": "https://github.com/org/repo",
                })
                self.assertEqual(self.graph.node("new")["description"], "Last")
                self.assertEqual(len(self.graph.edges), 2)
                for key, value in before_edge.items():
                    self.assertEqual(self.graph.edges[key], value)

    def test_valid_json_with_unsupported_property_types_is_skipped_per_row(self):
        for invalid in ({"nested": {"x": 1}}, {"values": [None]}, {"values": ["x", 1]},
                        {"values": [[1]]}, {"count": 2**63}):
            with self.subTest(properties=invalid):
                self.graph = StrictPropertyGraph()
                self.nodes("a_nodes.csv", [
                    ("good", "Repository", {"name": "Good"}),
                    ("bad", "Repository", {"name": "Bad", **invalid}),
                ])
                self.nodes("z_nodes.csv", [("last", "Repository", {"name": "Last"})])
                report = load_csv_dir(self.graph, self.directory)
                self.assertEqual((report.nodes_loaded, report.nodes_skipped), (2, 1))
                self.assertEqual(self.graph.node("last")["name"], "Last")
                self.assertNotIn(self.graph.key("Repository", "bad"), self.graph.nodes)

    def test_secondary_label_cannot_clear_repository_name(self):
        self.graph.upsert_nodes_batch("Repository:Reviewed", [("R", {"name": "Kept", "description": "Old"})])
        before = deepcopy(self.graph.nodes)
        path = self.nodes("reviewed_nodes.csv", [("R", "Reviewed", {"name": None, "description": "Rejected"})])
        report = load_nodes_from_csv(self.graph, str(path))
        self.assertEqual(report.nodes_skipped, 1)
        self.assertEqual(self.graph.nodes, before)

    def test_bad_properties_reject_the_whole_existing_update_and_keep_following_file(self):
        self.graph.upsert_nodes_batch("Repository", [("R", {
            "name": "Kept", "description": "Old", "topics": ["tested"],
        })])
        self.nodes("a_nodes.csv", [("R", "Repository", {
            "name": "Must not replace", "description": {"invalid": True},
        })])
        self.nodes("z_nodes.csv", [("P", "Person", {"name_raw": "Later"})])
        report = load_csv_dir(self.graph, self.directory)
        self.assertEqual((report.nodes_loaded, report.nodes_skipped), (1, 1))
        self.assertEqual(self.graph.node("R"), {
            "id": "R", "name": "Kept", "description": "Old", "topics": ["tested"],
        })
        self.assertEqual(self.graph.node("P", "Person")["name_raw"], "Later")
        self.assertEqual(report.issues[0]["id"], "R")
        self.assertTrue(report.issues[0]["reason"])

    def test_invalid_relationship_properties_do_not_block_valid_edges_or_following_file(self):
        self.graph.upsert_nodes_batch("Repository", [("R", {"name": "Repo"})])
        self.graph.upsert_nodes_batch("Publication", [("W1", {}), ("W2", {})])
        self.edges("a_rels.csv", [
            ("R", "W1", "Repository", "Publication", "IMPLEMENTS", {"evidence": {"invalid": True}}),
            ("R", "W1", "Repository", "Publication", "IMPLEMENTS", {"evidence": "Kept"}),
            ("R", "W1", "Repository", "Publication", "IMPLEMENTS", {"evidence": [None]}),
        ])
        self.edges("z_rels.csv", [
            ("R", "W2", "Repository", "Publication", "IMPLEMENTS", {"weight": 2}),
        ])
        report = load_csv_dir(self.graph, self.directory)
        self.assertEqual((report.relationships_loaded, report.relationships_skipped), (2, 2))
        self.assertEqual(len(self.graph.edges), 2)
        self.assertEqual(self.graph.edges["Repository", "R", "IMPLEMENTS", "Publication", "W1"],
                         {"evidence": "Kept"})

    def test_valid_property_boundaries_and_explicit_null_removal_survive(self):
        self.graph.upsert_nodes_batch("Repository", [("R", {"name": "Kept", "description": "Remove"})])
        values = {
            "description": None, "negative": -(2**63), "positive": 2**63 - 1,
            "enabled": True, "flags": [True, False], "empty": [], "numbers": [1, 2.5], "text": ["", "Пример"],
        }
        path = self.nodes("values_nodes.csv", [("R", "Repository", values)])
        report = load_nodes_from_csv(self.graph, str(path))
        self.assertEqual((report.nodes_loaded, report.nodes_skipped), (1, 0))
        self.assertEqual(self.graph.node("R")["name"], "Kept")
        self.assertNotIn("description", self.graph.node("R"))
        for key, value in values.items():
            if key != "description":
                self.assertEqual(self.graph.node("R")[key], value)

    def test_nonfinite_json_numbers_do_not_reach_the_driver(self):
        self.nodes("numbers_nodes.csv", [
            ("bad1", "Repository", {"name": "Bad", "weight": float("nan")}),
            ("bad2", "Repository", {"name": "Bad", "weight": float("inf")}),
            ("bad3", "Repository", {"name": "Bad", "weight": float("-inf")}),
            ("good", "Repository", {"name": "Good", "weight": 1.5}),
        ])
        report = load_csv_dir(self.graph, self.directory)
        self.assertEqual((report.nodes_loaded, report.nodes_skipped), (1, 3))
        self.assertEqual(len(self.graph.nodes), 1)

    def test_secondary_label_valid_name_update_and_person_name_are_not_rejected(self):
        self.graph.upsert_nodes_batch("Repository:Reviewed", [("R", {"name": "Before"})])
        self.graph.upsert_nodes_batch("Person", [("P", {"name": "Optional"})])
        self.nodes("mixed_nodes.csv", [
            ("R", "Reviewed", {"name": "After"}),
            ("P", "Person", {"name": None}),
        ])
        report = load_csv_dir(self.graph, self.directory)
        self.assertEqual((report.nodes_loaded, report.nodes_skipped), (2, 0))
        self.assertEqual(self.graph.node("R")["name"], "After")
        self.assertNotIn("name", self.graph.node("P", "Person"))

    def test_missing_repository_in_realistic_driver_result_is_a_row_error(self):
        client = object.__new__(Neo4jClient)
        client.driver = MagicMock()
        tx = Mock()
        tx.run.return_value = DriverRows([])
        session = client.driver.session.return_value.__enter__.return_value
        session.execute_write.side_effect = lambda action: action(tx)
        with self.assertRaises(RepositoryNameError):
            client.upsert_csv_node("Repository", "missing", {"description": "No name"})
        self.assertEqual(tx.run.call_count, 1)

    def test_secondary_label_partial_updates_keep_file_order_across_batches(self):
        for batch_size in (1, 2, 100):
            with self.subTest(batch_size=batch_size):
                self.graph = StrictPropertyGraph()
                self.graph.upsert_nodes_batch("Repository:Reviewed:Archived", [("R", {
                    "name": "Kept", "description": "Initial",
                })])
                path = self.nodes("ordered_nodes.csv", [
                    ("R", "Reviewed", {"description": "First"}),
                    ("R", "Archived", {"description": "Second"}),
                    ("R", "Reviewed", {"description": "Last"}),
                ])
                report = load_nodes_from_csv(self.graph, str(path), batch_size=batch_size)
                self.assertEqual((report.nodes_loaded, report.nodes_skipped), (3, 0))
                self.assertEqual(self.graph.node("R")["name"], "Kept")
                self.assertEqual(self.graph.node("R")["description"], "Last")


if __name__ == "__main__":
    unittest.main()
