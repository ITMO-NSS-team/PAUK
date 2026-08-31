import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mongomock

from pauk.cache.graph_snapshot import write_snapshot
from pauk.gui import rebuild
from pauk.gui.generate_data import write_graph_files
from pauk.jobs import locks
from pauk.jobs.models import GRAPH
from pauk.settings import Settings


def snapshot() -> dict[str, list]:
    """A graph snapshot shaped exactly the way `load_db` writes one.

    Persons and departments come back keyed by column name, everything else
    as positional tuples — mixing the two up is the kind of thing that only
    shows when the map comes out empty.
    """
    return {
        "persons": [
            {"id": "A1", "first_name_ru": "Иван", "second_name_ru": "Петрович",
             "surname_ru": "Петров", "name_ru": "Петров Иван Петрович",
             "name_variants": ["И. П. Петров"], "name_en": "Ivan Petrov",
             "degree": "к.т.н.", "github": "octocat",
             "orcid": "0000-0002-1825-0097"},
        ],
        "publications": [
            ("W1", "Статья про графы", "Журнал", "10.1000/x", "2024-05-01", 2024, True,
             "https://github.com/org/repo"),
        ],
        "repositories": [
            ("R1", "repo", "https://github.com/org/repo", "описание", 42, "octocat"),
        ],
        "departments": [{"id": "D1", "name_ru": "Кафедра", "name_en": "Department"}],
        "authorship": [("W1", "A1")],
        "person_depts": [("A1", "D1")],
        "pub_depts": [("W1", "D1")],
        "repo_pubs": [("R1", "W1")],
        "repo_persons": [("R1", "A1", "contributor")],
        "repo_depts": [("R1", "D1")],
    }


class WriteGraphFilesTest(unittest.TestCase):
    """The build that used to live inside `main()`.

    Pulled out so the maintenance worker can rebuild the map by calling a
    function instead of assembling a command line out of a form.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.snapshot = self.tmp / "graph_snapshot.json"
        write_snapshot(self.snapshot, snapshot())

    def build(self, public=False, out=None):
        out = out or self.tmp / ("public" if public else "private")
        return out, write_graph_files(self.snapshot, out, seed=42, public=public)

    def test_it_writes_both_files(self):
        out, _ = self.build()
        self.assertEqual(sorted(path.name for path in out.iterdir()),
                         ["graph-data.js", "graph-search.js"])

    def test_it_creates_the_directory(self):
        out = self.tmp / "does" / "not" / "exist"
        self.build(out=out)
        self.assertTrue((out / "graph-data.js").is_file())

    def test_it_reports_what_it_wrote(self):
        _, counts = self.build()
        self.assertEqual(counts["map_authors"], 1)
        self.assertEqual(counts["map_pubs"], 1)

    def test_the_department_count_is_the_map_s_own(self):
        # The map adds a bucket for authors with no department, so its
        # count is one more than the graph holds. Another reason the two
        # halves are reported under separate names.
        _, counts = self.build()
        self.assertEqual(counts["map_departments"], 2)

    def test_the_data_file_is_the_shape_the_page_expects(self):
        out, _ = self.build()
        text = (out / "graph-data.js").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("window.GRAPH="))
        json.loads(text[len("window.GRAPH="):])

    def test_the_search_file_keeps_its_wrapper(self):
        # The page reads it through window._onDetailReady; a rebuild that
        # dropped the wrapper would load and do nothing at all.
        out, _ = self.build()
        text = (out / "graph-search.js").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("(function(){var d="))
        self.assertTrue(text.endswith(
            ";if(typeof window._onDetailReady==='function')window._onDetailReady(d);"
            "else window._pendingDetail=d;})();"))

    def test_a_private_build_keeps_the_names(self):
        out, _ = self.build(public=False)
        text = (out / "graph-data.js").read_text(encoding="utf-8")
        self.assertIn("Петров", text)

    def test_a_public_build_drops_the_personal_fields(self):
        out, _ = self.build(public=True)
        text = (out / "graph-data.js").read_text(encoding="utf-8")
        for personal in ("Петров", "Ivan Petrov", "0000-0002-1825-0097", "к.т.н."):
            with self.subTest(field=personal):
                self.assertNotIn(personal, text)

    def test_the_same_seed_gives_the_same_map(self):
        # People navigate the map by shape. A rebuild that moved everything
        # would be a new map, not an updated one.
        first, _ = self.build(out=self.tmp / "one")
        second, _ = self.build(out=self.tmp / "two")
        self.assertEqual((first / "graph-data.js").read_text(),
                         (second / "graph-data.js").read_text())


class RebuildMapTest(unittest.TestCase):
    """The three steps as one call, with Neo4j standing in for itself."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.snapshot = self.tmp / "graph_snapshot.json"
        write_snapshot(self.snapshot, snapshot())
        self.config = Settings(map_dir=self.tmp / "map")
        # The rebuild holds the graph while it reads it, the same way a
        # publish holds it while it writes: both would otherwise picture a
        # graph half-written.
        self.db = mongomock.MongoClient()["pauk_test"]
        self.stats = {"graph_nodes": 3, "graph_rels": 5, "checks": 7, "checks_failed": 0}

    def rebuild(self, **kwargs):
        """Rebuild into the temporary map directory, and nowhere else.

        The guard is not paranoia: `public/` is a committed artefact, and a
        rebuild that ignored the settings it was handed would overwrite it
        from a test run. That happened once while breaking this on purpose.
        """
        write = rebuild.write_graph_files

        def guarded(snapshot_path, out_dir, **inner):
            # Checked before it writes, not after: asserting afterwards
            # still leaves the files overwritten.
            self.assertTrue(Path(out_dir).is_relative_to(self.tmp),
                            f"сборка ушла мимо временного каталога: {out_dir}")
            return write(snapshot_path, out_dir, **inner)

        with patch.object(rebuild, "write_graph_files", guarded), \
                patch.object(rebuild, "write_stats", return_value=self.stats):
            return rebuild.rebuild_map(self.config, self.db,
                                       snapshot_path=self.snapshot, **kwargs)

    def test_a_given_snapshot_is_not_exported_again(self):
        with patch.object(rebuild, "GraphSnapshotExporter") as exporter, \
                patch.object(rebuild, "write_stats", return_value=self.stats):
            rebuild.rebuild_map(self.config, self.db, snapshot_path=self.snapshot)
        exporter.assert_not_called()

    def test_it_writes_into_the_configured_directory(self):
        self.rebuild()
        self.assertTrue((self.config.map_out_dir(False) / "graph-data.js").is_file())

    def test_public_and_private_go_to_different_places(self):
        self.rebuild(public=True)
        self.assertTrue((self.config.map_out_dir(True) / "graph-data.js").is_file())
        self.assertFalse((self.config.map_out_dir(False) / "graph-data.js").is_file())

    def test_the_counts_of_both_halves_come_back(self):
        counts = self.rebuild()
        self.assertEqual(counts["map_authors"], 1)
        self.assertEqual(counts["graph_nodes"], 3)

    def test_the_graph_is_held_while_it_is_read(self):
        held = []
        write = rebuild.write_graph_files

        def watching(snapshot_path, out_dir, **inner):
            held.append(locks.holder(self.db, GRAPH))
            return write(snapshot_path, out_dir, **inner)

        with patch.object(rebuild, "write_graph_files", watching):
            self.rebuild()
        self.assertIsNotNone(held[0], "граф не был занят во время пересборки")

    def test_the_graph_is_free_again_afterwards(self):
        self.rebuild()
        self.assertIsNone(locks.holder(self.db, GRAPH))

    def test_a_rebuild_waits_for_a_publish(self):
        with locks.held(self.db, GRAPH, "publisher"), self.assertRaises(locks.Busy):
            rebuild.rebuild_map(self.config, self.db, snapshot_path=self.snapshot)

    def test_the_map_and_the_graph_are_counted_apart(self):
        # The map leaves out publications with no ITMO author, so one number
        # for both would be wrong on every real graph.
        counts = self.rebuild()
        self.assertIn("map_pubs", counts)
        self.assertIn("graph_nodes", counts)


class MapPathTest(unittest.TestCase):
    """Everything that touches the map's files must agree where they are."""

    def test_the_server_reads_where_the_rebuild_writes(self):
        # serve.py reads sys.argv at import time — it is a script first —
        # so it is imported here with an argv of its own.
        import importlib
        import sys
        with patch.object(sys, "argv", ["serve.py"]):
            serve = importlib.reload(importlib.import_module("pauk.gui.serve"))
        self.assertEqual(serve.DATA_DIR, Settings().map_out_dir(serve.PUBLIC))

    def test_the_stats_are_written_where_the_server_reads(self):
        from pauk.gui.generate_stats import OUT_DIR_DEFAULT
        self.assertEqual(OUT_DIR_DEFAULT, Settings().map_out_dir(False))

    def test_the_default_is_where_the_files_have_always_been(self):
        # `public/` is committed — the GitHub Pages build is an artefact of
        # this repository, and moving it would drop it from the deploy.
        expected = Path(__file__).resolve().parents[2] / "pauk" / "gui" / "data"
        self.assertEqual(Settings().map_dir, expected)


class EmptyGraphTest(unittest.TestCase):
    """A graph with no ITMO authors is an empty map, not a failure.

    cKDTree refuses an empty array, so the layout step raised ValueError and
    the rebuild died on a fresh database or a group nobody had published.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.snapshot = self.tmp / "graph_snapshot.json"

    def build(self, db):
        write_snapshot(self.snapshot, db)
        return write_graph_files(self.snapshot, self.tmp / "out", seed=42)

    @staticmethod
    def blank() -> dict[str, list]:
        return {"persons": [], "publications": [], "repositories": [], "departments": [],
                "authorship": [], "person_depts": [], "pub_depts": [],
                "repo_pubs": [], "repo_persons": [], "repo_depts": []}

    def test_an_empty_graph_builds(self):
        counts = self.build(self.blank())
        self.assertEqual(counts["map_authors"], 0)

    def test_a_publication_with_no_itmo_author_builds(self):
        db = self.blank()
        db["publications"] = [("W1", "Статья", None, None, None, 2024, False, None)]
        self.assertEqual(self.build(db)["map_pubs"], 0)

    def test_departments_without_people_build(self):
        db = self.blank()
        db["departments"] = [{"id": f"D{n}", "name_ru": f"Кафедра {n}", "name_en": ""}
                             for n in range(4)]
        db["publications"] = [("W1", "Статья", None, None, None, 2024, False, None)]
        db["pub_depts"] = [("W1", "D0")]
        self.assertEqual(self.build(db)["map_authors"], 0)

    def test_the_files_are_written_even_when_empty(self):
        self.build(self.blank())
        out = self.tmp / "out"
        self.assertEqual(sorted(path.name for path in out.iterdir()),
                         ["graph-data.js", "graph-search.js"])

