import csv
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from pauk.graph.csv_loader import load_csv_dir, load_nodes_from_csv, load_relationships_from_csv
from pauk.graph.load import main
from pauk.graph.validation import validate_repository_name


class MemoryGraph:
    """Stateful import double retaining omitted properties and endpoint matching."""

    def __init__(self):
        self.nodes = {}
        self.edges = {}

    def key(self, labels, node_id):
        wanted = frozenset(labels.split(":"))
        return next((key for key in self.nodes if key[1] == node_id and wanted <= key[0]),
                    (wanted, node_id))

    def upsert_nodes_batch(self, labels, rows):
        for node_id, props in rows:
            stored = self.nodes.setdefault(self.key(labels, node_id), {"id": node_id})
            for name, value in props.items():
                if name in ("id", "created_at", "updated_at"):
                    continue
                if value is None:
                    stored.pop(name, None)
                else:
                    stored[name] = deepcopy(value)

    def upsert_csv_node(self, labels, node_id, props):
        key = self.key(labels, node_id)
        previous = self.nodes.get(key, {})
        if "Repository" in key[0]:
            validate_repository_name(props["name"] if "name" in props else previous.get("name"))
        self.upsert_nodes_batch(labels, [(node_id, props)])

    def upsert_relationships_batch(self, src_label, tgt_label, rel_type, rows):
        count = 0
        combined = {}
        for source, target, props in rows:
            if self.key(src_label, source) not in self.nodes or self.key(tgt_label, target) not in self.nodes:
                continue
            key = (src_label, source, rel_type, tgt_label, target)
            stored = combined.setdefault(key, {})
            corresponding = stored.get("is_corresponding") or props.get("is_corresponding")
            stored.update(props)
            if rel_type == "AUTHORED":
                stored["is_corresponding"] = corresponding
            count += 1
        for key, props in combined.items():
            self.edges.setdefault(key, {}).update(props)
        return count

    def node(self, node_id, labels="Repository"):
        return self.nodes[self.key(labels, node_id)]


class CsvRepositoryNamesTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.directory = Path(self.tempdir.name)
        self.client = MemoryGraph()

    def write_nodes(self, filename, rows, raw=False):
        path = self.directory / filename
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["id", "labels", "properties"])
            for node_id, labels, properties in rows:
                writer.writerow([node_id, labels, properties if raw else json.dumps(properties)])
        return path

    def write_edges(self, rows, filename="links_rels.csv", raw=False):
        path = self.directory / filename
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["start_id", "end_id", "src_label", "tgt_label", "type", "properties"])
            for *keys, props in rows:
                writer.writerow([*keys, props if raw else json.dumps(props)])
        return path

    def test_invalid_repository_rows_do_not_block_valid_neighbors(self):
        invalid = ({}, {"name": None}, {"name": ""}, {"name": " \t\n"}, {"name": 0}, [])
        for batch_size in (1, 100):
            with self.subTest(batch_size=batch_size):
                self.client = MemoryGraph()
                rows = [("first", "Repository", {"name": "First"})]
                rows += [(f"bad{i}", "Repository", props) for i, props in enumerate(invalid)]
                rows += [("last", "Repository", {"name": "Last"})]
                path = self.write_nodes("repositories_nodes.csv", rows)
                report = load_nodes_from_csv(self.client, str(path), batch_size=batch_size)
                self.assertEqual((report.nodes_loaded, report.nodes_skipped), (2, 6))
                self.assertEqual(len(self.client.nodes), 2)
                self.assertEqual([item["id"] for item in report.issues], [f"bad{i}" for i in range(6)])
                self.assertTrue(all(item["file"] == str(path) for item in report.issues))
                self.assertTrue(all(item["line"] >= 3 and item["reason"] for item in report.issues))

    def test_malformed_json_and_nonobjects_allow_next_rows_and_files(self):
        self.write_nodes("a_nodes.csv", [
            ("R1", "Repository", '{"name":"First"}'),
            ("broken", "Repository", '{'),
            ("null", "Person", 'null'),
            ("list", "Person", '[]'),
            ("R2", "Repository", '{"name":"Second"}'),
        ], raw=True)
        self.write_nodes("z_nodes.csv", [("P1", "Person", {"name_raw": "Person"})])
        self.write_edges([("P1", "R2", "Person", "Repository", "CONTRIBUTED_TO", {})])
        report = load_csv_dir(self.client, self.directory)
        self.assertEqual((report.nodes_loaded, report.nodes_skipped), (3, 3))
        self.assertEqual((report.relationships_loaded, report.relationships_skipped), (1, 0))
        self.assertEqual(len(self.client.edges), 1)

    def test_partial_update_preserves_name_url_and_other_properties(self):
        self.client.upsert_nodes_batch("Repository", [("R1", {
            "name": "Keep", "url": "https://github.com/a/b", "topics": ["x"], "description": "Old",
        })])
        path = self.write_nodes("repos_nodes.csv", [("R1", "Repository", {"description": "New"})])
        report = load_nodes_from_csv(self.client, str(path))
        self.assertEqual(report.nodes_loaded, 1)
        self.assertEqual(self.client.node("R1"), {
            "id": "R1", "name": "Keep", "url": "https://github.com/a/b", "topics": ["x"],
            "description": "New",
        })

    def test_explicit_invalid_name_rejects_entire_update(self):
        for value in (None, "", " \t", 1, False, [], {}):
            with self.subTest(name=value):
                self.client.upsert_nodes_batch("Repository", [("R1", {"name": "Keep", "description": "Before"})])
                before = deepcopy(self.client.nodes)
                path = self.write_nodes("repos_nodes.csv", [("R1", "Repository", {
                    "name": value, "description": "Must not be written",
                })])
                report = load_nodes_from_csv(self.client, str(path))
                self.assertEqual(report.nodes_skipped, 1)
                self.assertEqual(self.client.nodes, before)

    def test_existing_unnamed_repository_requires_repair(self):
        self.client.upsert_nodes_batch("Repository", [("R1", {"description": "Before"})])
        path = self.write_nodes("repos_nodes.csv", [
            ("R1", "Repository", {"description": "Rejected"}),
            ("R1", "Repository", {"name": "Repaired"}),
            ("R1", "Repository", {"description": "After"}),
        ])
        report = load_nodes_from_csv(self.client, str(path))
        self.assertEqual((report.nodes_loaded, report.nodes_skipped), (2, 1))
        self.assertEqual(self.client.node("R1"), {"id": "R1", "name": "Repaired", "description": "After"})

    def test_ordered_create_then_partial_update_across_batches_and_files(self):
        for batch_size in (1, 2, 100):
            with self.subTest(batch_size=batch_size):
                self.client = MemoryGraph()
                path = self.write_nodes("a_nodes.csv", [
                    ("R1", "Repository:Reviewed", {"name": "Keep"}),
                    ("R1", "Reviewed:Repository", {"description": "First"}),
                ])
                report = load_nodes_from_csv(self.client, str(path), batch_size=batch_size)
                self.assertEqual(report.nodes_loaded, 2)
                path = self.write_nodes("b_nodes.csv", [("R1", "Repository", {"description": "Second"})])
                load_nodes_from_csv(self.client, str(path), batch_size=batch_size)
                self.assertEqual(self.client.node("R1")["description"], "Second")
                self.assertEqual(self.client.node("R1")["name"], "Keep")

    def test_missing_multilabel_match_does_not_borrow_name(self):
        self.client.upsert_nodes_batch("Repository", [("R1", {"name": "Keep"})])
        before = deepcopy(self.client.nodes)
        path = self.write_nodes("repos_nodes.csv", [("R1", "Reviewed:Repository", {"description": "New"})])
        report = load_nodes_from_csv(self.client, str(path))
        self.assertEqual(report.nodes_skipped, 1)
        self.assertEqual(self.client.nodes, before)

    def test_queued_generic_node_is_flushed_before_repository_patch(self):
        self.client.upsert_nodes_batch("Repository:Reviewed", [("R1", {"name": "Old"})])
        path = self.write_nodes("repos_nodes.csv", [
            ("R1", "Reviewed", {"name": "New"}),
            ("R1", "Repository", {"description": "Updated"}),
        ])
        report = load_nodes_from_csv(self.client, str(path))
        self.assertEqual(report.nodes_loaded, 2)
        self.assertEqual(self.client.node("R1")["name"], "New")

    def test_skipped_creation_counts_missing_edge_but_existing_endpoint_still_works(self):
        self.client.upsert_nodes_batch("Repository", [("existing", {"name": "Keep"})])
        self.write_nodes("nodes_nodes.csv", [
            ("P1", "Person", {}),
            ("missing", "Repository", {}),
            ("existing", "Repository", {"name": None}),
        ])
        self.write_edges([
            ("P1", "missing", "Person", "Repository", "CONTRIBUTED_TO", {}),
            ("P1", "existing", "Person", "Repository", "CONTRIBUTED_TO", {}),
        ])
        report = load_csv_dir(self.client, self.directory)
        self.assertEqual((report.nodes_loaded, report.nodes_skipped), (1, 2))
        self.assertEqual((report.relationships_loaded, report.relationships_skipped), (1, 1))
        self.assertEqual(len(self.client.edges), 1)
        self.assertIn("missing endpoints", report.issues[-1]["reason"])
        again = load_csv_dir(self.client, self.directory)
        self.assertEqual(again.relationships_loaded, 1)
        self.assertEqual(len(self.client.edges), 1)

    def test_malformed_relationships_continue_to_following_file(self):
        self.client.upsert_nodes_batch("Person", [("P1", {}), ("P2", {})])
        self.client.upsert_nodes_batch("Publication", [("W1", {})])
        self.write_edges([
            ("P1", "W1", "Person", "Publication", "AUTHORED", '{'),
            ("P1", "W1", "Person", "Publication", "AUTHORED", 'null'),
            ("P1", "W1", "Person", "Publication", "AUTHORED", '{"is_corresponding":true}'),
            ("P1", "W1", "Person", "Publication", "AUTHORED", '{"is_corresponding":false}'),
        ], raw=True, filename="a_rels.csv")
        self.write_edges([("P2", "W1", "Person", "Publication", "AUTHORED", {})], filename="z_rels.csv")
        report = load_csv_dir(self.client, self.directory)
        self.assertEqual((report.relationships_loaded, report.relationships_skipped), (3, 2))
        self.assertTrue(self.client.edges["Person", "P1", "AUTHORED", "Publication", "W1"]["is_corresponding"])

    def test_diagnostics_use_physical_line_numbers_for_multiline_csv(self):
        path = self.write_nodes("repos_nodes.csv", [
            ("R1", "Repository", '{\n"name": "Valid"\n}'),
            ("bad", "Repository", '{'),
        ], raw=True)
        report = load_nodes_from_csv(self.client, str(path))
        self.assertEqual(report.issues[0]["line"], 5)
        self.assertEqual(report.issues[0]["id"], "bad")

    def test_bad_ids_labels_and_missing_columns_are_reported(self):
        path = self.write_nodes("nodes_nodes.csv", [
            ("", "Person", {}), ("P1", "", {}), ("P2", "Person) DELETE n", {}),
            ("P3", "Person", {}),
        ])
        report = load_nodes_from_csv(self.client, str(path))
        self.assertEqual((report.nodes_loaded, report.nodes_skipped), (1, 3))
        path.write_text("id,properties\nP4,{}\n", encoding="utf-8")
        report = load_nodes_from_csv(self.client, str(path))
        self.assertEqual(report.nodes_skipped, 1)

    def test_database_failures_are_not_reported_as_bad_rows(self):
        path = self.write_nodes("repos_nodes.csv", [("R1", "Repository", {"name": "Good"})])
        with (
            patch.object(self.client, "upsert_csv_node", side_effect=RuntimeError("database unavailable")),
            self.assertRaisesRegex(RuntimeError, "database unavailable"),
        ):
            load_csv_dir(self.client, self.directory)
        path = self.write_edges([("P1", "R1", "Person", "Repository", "CONTRIBUTED_TO", {})])
        with (
            patch.object(self.client, "upsert_relationships_batch", side_effect=RuntimeError("database unavailable")),
            self.assertRaisesRegex(RuntimeError, "database unavailable"),
        ):
            load_relationships_from_csv(self.client, str(path))

    def test_driver_value_error_is_not_misclassified_as_an_invalid_name(self):
        path = self.write_nodes("repos_nodes.csv", [("R1", "Repository", {"name": "Good"})])
        with (
            patch.object(self.client, "upsert_csv_node", side_effect=ValueError("driver configuration")),
            self.assertRaisesRegex(ValueError, "driver configuration"),
        ):
            load_nodes_from_csv(self.client, str(path))

    def test_cli_loads_valid_rows_and_closes_connection(self):
        self.write_nodes("repos_nodes.csv", [("bad", "Repository", {}), ("good", "Repository", {"name": "Good"})])
        self.client.close = Mock()
        with (
            patch("sys.argv", ["pauk.graph.load", "--dir", str(self.directory)]),
            patch("pauk.graph.load.Neo4jClient", return_value=self.client),
            patch("pauk.graph.load.create_constraints"),
            self.assertLogs("pauk.graph.csv_loader", level="INFO") as logs,
        ):
            main()
        self.client.close.assert_called_once()
        self.assertEqual(self.client.node("good")["name"], "Good")
        self.assertIn("nodes loaded=1 skipped=1", logs.output[-1])

    def test_cli_closes_connection_after_database_failure(self):
        self.client.close = Mock()
        with (
            patch("sys.argv", ["pauk.graph.load", "--dir", str(self.directory)]),
            patch("pauk.graph.load.Neo4jClient", return_value=self.client),
            patch("pauk.graph.load.create_constraints", side_effect=RuntimeError("offline")),
            self.assertRaisesRegex(RuntimeError, "offline"),
        ):
            main()
        self.client.close.assert_called_once()

    def test_invalid_batch_size_does_not_write(self):
        for loader in (load_nodes_from_csv, load_relationships_from_csv):
            with self.subTest(loader=loader.__name__), self.assertRaises(ValueError):
                loader(self.client, "unused.csv", batch_size=0)
        self.assertFalse(self.client.nodes)


if __name__ == "__main__":
    unittest.main()
