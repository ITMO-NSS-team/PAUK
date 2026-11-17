"""Reject incompatible snapshots before expensive map work or atomic replacement."""

import json
import tempfile
import unittest
from pathlib import Path

from pauk.cache.graph_snapshot import read_snapshot, write_snapshot
from pauk.cache.snapshot_schema import SNAPSHOT_COLUMNS, validate_snapshot
from pauk.cache.tests.fixtures import complete_snapshot


class SnapshotValidationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "snapshot.json"
        self.graph = complete_snapshot({"repositories": [{"id": "R1"}]})

    def read(self, graph):
        self.path.write_text(json.dumps(graph), encoding="utf-8")
        return read_snapshot(self.path)

    def test_round_trip_preserves_nulls_extra_columns_and_empty_tables(self):
        self.graph["repositories"][0]["future_property"] = 42
        self.graph["future_table"] = []
        write_snapshot(self.path, self.graph)
        self.assertEqual(read_snapshot(self.path), self.graph)

    def test_empty_tables_are_valid(self):
        validate_snapshot({name: [] for name in SNAPSHOT_COLUMNS})

    def test_missing_table_and_legacy_wrapper_are_rejected(self):
        for graph in ({}, {"schema_version": 3, "graph": self.graph}):
            with self.subTest(graph=graph), self.assertRaisesRegex(ValueError, "missing tables.*pauk cache export"):
                self.read(graph)

    def test_non_object_root_is_rejected(self):
        for graph in (None, [], 1, "text"):
            with self.subTest(graph=graph), self.assertRaisesRegex(ValueError, "expected a JSON object"):
                self.read(graph)

    def test_table_must_be_a_list(self):
        for rows in (None, {}, "text"):
            with self.subTest(rows=rows), self.assertRaisesRegex(ValueError, "repositories.*must be a list"):
                self.read({**self.graph, "repositories": rows})

    def test_each_row_must_be_an_object(self):
        with self.assertRaisesRegex(ValueError, "repositories.*row 2 must be an object"):
            self.read({**self.graph, "repositories": [self.graph["repositories"][0], []]})

    def test_missing_column_in_second_row_is_rejected(self):
        for column in ("forks_num", "is_fork", "owner_type"):
            row = dict(self.graph["repositories"][0])
            del row[column]
            with self.subTest(column=column), self.assertRaisesRegex(ValueError, f"repositories.*row 2.*{column}"):
                self.read({**self.graph, "repositories": [self.graph["repositories"][0], row]})

    def test_missing_name_column_is_not_treated_as_null(self):
        graph = complete_snapshot({"persons": [{"id": "A1"}]})
        del graph["persons"][0]["surname_en"]
        with self.assertRaisesRegex(ValueError, "persons.*surname_en"):
            self.read(graph)

    def test_invalid_write_preserves_existing_snapshot(self):
        write_snapshot(self.path, self.graph)
        original = self.path.read_bytes()
        with self.assertRaises(ValueError):
            write_snapshot(self.path, {"repositories": []})
        self.assertEqual(self.path.read_bytes(), original)
