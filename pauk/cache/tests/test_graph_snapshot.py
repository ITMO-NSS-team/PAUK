"""Unit tests for graph_snapshot.py: write/read round trip, snapshot lookup."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from pauk.cache.graph_snapshot import (
    dated_snapshot_path,
    latest_snapshot,
    read_snapshot,
    write_snapshot,
)


class WriteReadSnapshotTest(unittest.TestCase):
    def test_round_trip_preserves_graph_unchanged(self):
        graph = {"persons": [{"id": "p1", "is_itmo": True}], "publications": []}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.json"
            write_snapshot(path, graph)
            self.assertEqual(read_snapshot(path), graph)

    def test_read_snapshot_rejects_non_object_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.json"
            path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
            with self.assertRaises(ValueError):
                read_snapshot(path)


class DatedSnapshotPathTest(unittest.TestCase):
    def test_embeds_todays_date_in_the_filename(self):
        path = dated_snapshot_path(Path("/data/cache"))
        self.assertEqual(path.parent, Path("/data/cache"))
        self.assertEqual(path.name, f"graph_snapshot_{date.today():%d-%m-%Y}.json")


class LatestSnapshotTest(unittest.TestCase):
    def test_raises_when_no_snapshot_exists(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(FileNotFoundError):
            latest_snapshot(Path(tmp))

    def test_picks_the_file_with_the_latest_date_in_its_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            older = cache_dir / "graph_snapshot_01-01-2026.json"
            newer = cache_dir / "graph_snapshot_15-06-2026.json"
            # Written in the opposite order, to prove this isn't picking by mtime.
            newer.write_text("{}", encoding="utf-8")
            older.write_text("{}", encoding="utf-8")

            self.assertEqual(latest_snapshot(cache_dir), newer)

    def test_ignores_files_that_dont_match_the_dated_naming_pattern(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            snapshot = cache_dir / "graph_snapshot_01-01-2026.json"
            snapshot.write_text("{}", encoding="utf-8")
            (cache_dir / "notes.txt").write_text("unrelated file", encoding="utf-8")
            (cache_dir / "graph_snapshot_v2.json").write_text("{}", encoding="utf-8")

            self.assertEqual(latest_snapshot(cache_dir), snapshot)


if __name__ == "__main__":
    unittest.main()
