import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pauk.graph.csv_loader import load_csv_dir, load_nodes_from_csv
from pauk.graph.load import main


class CsvRepositoryNamesTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.directory = Path(self.tempdir.name)
        self.client = Mock()
        self.written_nodes = []
        self.client.upsert_nodes_batch.side_effect = (
            lambda labels, rows: self.written_nodes.append((labels, list(rows)))
        )

    def write_nodes(self, filename, rows):
        path = self.directory / filename
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["id", "labels", "properties"])
            writer.writeheader()
            for node_id, labels, properties in rows:
                writer.writerow({"id": node_id, "labels": labels, "properties": json.dumps(properties)})
        return path

    def test_invalid_late_repository_does_not_write_earlier_batches(self):
        for invalid in ({}, {"name": None}, {"name": ""}, {"name": " \t\n"}, {"name": 0}, []):
            with self.subTest(properties=invalid):
                path = self.write_nodes("repositories_nodes.csv", [
                    ("R1", "Repository", {"name": "valid"}),
                    ("R2", "Repository", invalid),
                ])
                with self.assertRaisesRegex(ValueError, "Repository.name"):
                    load_nodes_from_csv(self.client, str(path), batch_size=1)
                self.client.upsert_nodes_batch.assert_not_called()

    def test_invalid_later_file_blocks_all_nodes_and_relationships(self):
        self.write_nodes("a_nodes.csv", [("P1", "Person", {"name_raw": "Known Person"})])
        self.write_nodes("z_nodes.csv", [("R1", "Repository", {"name": " "})])
        (self.directory / "links_rels.csv").write_text(
            "start_id,end_id,src_label,tgt_label,type,properties\n"
            "P1,R1,Person,Repository,CONTRIBUTED_TO,{}\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "z_nodes.csv:2"):
            load_csv_dir(self.client, self.directory)
        self.assertEqual(self.client.mock_calls, [])

    def test_cli_invalid_repository_does_not_connect_or_create_schema(self):
        self.write_nodes("repositories_nodes.csv", [("R1", "Repository", {"name": " "})])
        with (
            patch("sys.argv", ["pauk.graph.load", "--dir", str(self.directory)]),
            patch("pauk.graph.load.Neo4jClient") as client,
            patch("pauk.graph.load.create_constraints") as create_constraints,
            self.assertRaisesRegex(ValueError, "Repository.name"),
        ):
            main()
        client.assert_not_called()
        create_constraints.assert_not_called()

    def test_repository_multilabel_requires_name_in_either_position(self):
        for labels in ("Repository:Reviewed", "Reviewed:Repository"):
            with self.subTest(labels=labels):
                path = self.write_nodes("repositories_nodes.csv", [("R1", labels, {})])
                with self.assertRaisesRegex(ValueError, "Repository.name"):
                    load_nodes_from_csv(self.client, str(path))
                self.client.upsert_nodes_batch.assert_not_called()

    def test_valid_import_preserves_properties_and_relationships(self):
        properties = {"name": " repo ", "description": "", "stars_num": 0, "archived": False}
        self.write_nodes("repositories_nodes.csv", [("R1", "Repository:Reviewed", properties)])
        self.write_nodes("people_nodes.csv", [("P1", "Person", {"name_raw": "Known Person"})])
        (self.directory / "links_rels.csv").write_text(
            "start_id,end_id,src_label,tgt_label,type,properties\n"
            "P1,R1,Person,Repository,CONTRIBUTED_TO,{}\n",
            encoding="utf-8",
        )
        load_csv_dir(self.client, self.directory)
        self.assertIn(("Repository:Reviewed", [("R1", properties)]), self.written_nodes)
        self.client.upsert_relationships_batch.assert_called_once_with(
            "Person", "Repository", "CONTRIBUTED_TO", [("P1", "R1", {})],
        )

    def test_nonrepository_nodes_do_not_require_repository_name(self):
        path = self.write_nodes("people_nodes.csv", [("P1", "Person", {"name_raw": "Known Person"})])
        load_nodes_from_csv(self.client, str(path), batch_size=1)
        self.assertEqual(self.written_nodes, [("Person", [("P1", {"name_raw": "Known Person"})])])


if __name__ == "__main__":
    unittest.main()
