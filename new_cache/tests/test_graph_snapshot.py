"""Юнит-тесты для `graph_snapshot.py`: конверт версии схемы + свежесть файла."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from new_cache.graph_snapshot import SCHEMA_VERSION, is_fresh, read_snapshot, write_snapshot


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


class IsFreshTest(unittest.TestCase):
    def test_missing_file_is_never_fresh(self):
        """Отсутствующий путь — это не 'бесконечно старый' файл, который надо
        было бы как-то по-особому обрабатывать в вызывающем коде, а просто
        гарантированно не свежий."""
        self.assertFalse(is_fresh(Path("/несуществующий/путь/snapshot.json"), timedelta(days=1)))

    def test_freshly_written_file_is_fresh(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as fh:
            fh.write(b"{}")
            path = Path(fh.name)
        try:
            self.assertTrue(is_fresh(path, timedelta(hours=1)))
        finally:
            path.unlink()

    def test_old_file_is_not_fresh(self):
        """Подделываем mtime файла в прошлое вместо реального ожидания — тест
        детерминирован и не тратит время выполнения на настоящий sleep."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as fh:
            fh.write(b"{}")
            path = Path(fh.name)
        try:
            two_days_ago = path.stat().st_mtime - 2 * 24 * 3600
            os.utime(path, (two_days_ago, two_days_ago))
            self.assertFalse(is_fresh(path, timedelta(days=1)))
        finally:
            path.unlink()
