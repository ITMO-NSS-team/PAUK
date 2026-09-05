"""Юнит-тесты для `write_snapshot()`/`read_snapshot()` — конверта версии схемы."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from new_cache.graph_snapshot import SCHEMA_VERSION, read_snapshot, write_snapshot


class WriteReadSnapshotTest(unittest.TestCase):
    def test_round_trip_preserves_graph_unchanged(self):
        graph = {"persons": [{"id": "p1", "is_itmo": True}], "publications": []}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.json"
            write_snapshot(path, graph)
            self.assertEqual(read_snapshot(path), graph)

    def test_write_snapshot_stamps_schema_version_and_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.json"
            write_snapshot(path, {})
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
            self.assertIn("generated_at", payload)

    def test_read_snapshot_rejects_mismatched_schema_version(self):
        """Снепшот от несовместимой версии кода не должен молча скормиться
        сегодняшнему `build_graph_data()` — только явный ValueError."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.json"
            path.write_text(
                json.dumps({"schema_version": SCHEMA_VERSION + 1, "graph": {}}),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                read_snapshot(path)

    def test_read_snapshot_rejects_missing_graph_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.json"
            path.write_text(json.dumps({"schema_version": SCHEMA_VERSION}), encoding="utf-8")
            with self.assertRaises(ValueError):
                read_snapshot(path)
