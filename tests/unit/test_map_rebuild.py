import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mongomock

from pauk.cache.graph_snapshot import write_snapshot
from pauk.gui import rebuild
from pauk.gui.graph_builder.builder import write_site_data
from pauk.jobs import locks
from pauk.jobs.models import GRAPH
from pauk.settings import Settings

DETAIL_FILES = ["authors-detail.json", "graph-data.json", "pubs-detail.json", "repos-detail.json"]


def blank() -> dict[str, list]:
    """Every table `load_db` writes, all empty."""
    return {name: [] for name in (
        "persons", "publications", "repositories", "departments", "organizations", "authorship",
        "person_depts", "pub_depts", "repo_pubs", "mentions_repos", "mentions_candidates",
        "repo_persons", "repo_depts",
    )}


def publication(pid: str = "W1") -> dict:
    return {"id": pid, "title": "Статья про графы", "type": "article", "journal": "Журнал",
            "doi": "10.1000/x", "publication_date": "2024-05-01", "year": 2024, "has_code": True,
            "code_url": '["https://github.com/org/repo"]', "fields": [], "funding": "[]",
            "versions": "[]", "openalex_url": "", "abstract": ""}


def snapshot() -> dict[str, list]:
    """A graph snapshot shaped the way `load_db` writes one: rows keyed by column name."""
    return {
        **blank(),
        "persons": [
            {"id": "A1", "is_itmo": True, "openalex_id": "A1",
             "first_name_ru": "Иван", "second_name_ru": "Петрович", "surname_ru": "Петров",
             "first_name_en": "Ivan", "second_name_en": "Petrovich", "surname_en": "Petrov",
             "name_ru": "Петров Иван Петрович", "name_en": "Ivan Petrov",
             "name_variants": ["И. П. Петров"], "other_names": [], "degree": "к.т.н.",
             "github": "octocat", "orcid": "0000-0002-1825-0097", "google_scholar": None,
             "email": None, "affiliations": "[]"},
        ],
        "publications": [publication()],
        "repositories": [
            {"id": "R1", "name": "repo", "url": "https://github.com/org/repo",
             "description": "описание", "stars_num": 42, "has_readme": True, "license": "",
             "contributors": ["octocat"], "owner": "octocat", "owner_type": "user"},
        ],
        "departments": [{"id": "D1", "name_ru": "Кафедра", "name_en": "Department"}],
        "authorship": [{"pid": "W1", "per": "A1", "position": 1, "is_corresponding": True}],
        "person_depts": [{"per": "A1", "did": "D1"}],
        "pub_depts": [{"pid": "W1", "did": "D1"}],
        "repo_pubs": [{"rid": "R1", "pid": "W1"}],
        "repo_persons": [{"rid": "R1", "per": "A1", "role": "contributor"}],
        "repo_depts": [{"rid": "R1", "did": "D1"}],
    }


class WriteSiteDataTest(unittest.TestCase):
    """The build the worker calls - the same one `pauk gui build` runs."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.snapshot = self.tmp / "graph_snapshot.json"
        write_snapshot(self.snapshot, snapshot())

    def build(self, out=None):
        out = out or self.tmp / "gui"
        return out, write_site_data(self.snapshot, out, seed=42)

    def test_it_writes_both_builds_and_creates_the_directories(self):
        out, _ = self.build(self.tmp / "does" / "not" / "exist")
        self.assertEqual(sorted(p.name for p in (out / "private").iterdir()), DETAIL_FILES)
        self.assertEqual(sorted(p.name for p in (out / "public").iterdir()),
                         [name for name in DETAIL_FILES if name != "authors-detail.json"])

    def test_it_reports_what_it_wrote(self):
        _, counts = self.build()
        self.assertEqual(counts["map_authors"], 1)
        self.assertEqual(counts["map_pubs"], 1)
        # The map adds a bucket for entities with no department.
        self.assertEqual(counts["map_departments"], 2)

    def test_personal_fields_stay_out_of_the_public_build(self):
        out, _ = self.build()
        public = "".join(p.read_text(encoding="utf-8") for p in (out / "public").iterdir())
        for personal in ("0000-0002-1825-0097", "к.т.н.", "octocat@"):
            with self.subTest(field=personal):
                self.assertNotIn(personal, public)
        self.assertIn("0000-0002-1825-0097", (out / "private" / "authors-detail.json").read_text(encoding="utf-8"))

    def test_the_data_file_is_plain_json(self):
        out, _ = self.build()
        graph = json.loads((out / "private" / "graph-data.json").read_text(encoding="utf-8"))
        self.assertEqual([a["key"] for a in graph["authors"]], ["A1"])

    def test_the_same_seed_gives_the_same_map(self):
        # People navigate the map by shape. A rebuild that moved everything
        # would be a new map, not an updated one.
        first, _ = self.build(self.tmp / "one")
        second, _ = self.build(self.tmp / "two")
        self.assertEqual((first / "private" / "graph-data.json").read_text(encoding="utf-8"),
                         (second / "private" / "graph-data.json").read_text(encoding="utf-8"))


class RebuildMapTest(unittest.TestCase):
    """Snapshot and build as one call, with Neo4j standing in for itself."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.snapshot = self.tmp / "graph_snapshot.json"
        write_snapshot(self.snapshot, snapshot())
        self.config = Settings(data_dir=self.tmp / "data")
        # The rebuild holds the graph while it reads it, the same way a
        # publish holds it while it writes: both would otherwise picture a
        # graph half-written.
        self.db = mongomock.MongoClient()["pauk_test"]

    def rebuild(self):
        return rebuild.rebuild_map(self.config, self.db, snapshot_path=self.snapshot)

    def test_a_given_snapshot_is_not_exported_again(self):
        with patch.object(rebuild, "GraphSnapshotExporter") as exporter:
            self.rebuild()
        exporter.assert_not_called()

    def test_it_writes_into_the_configured_directory(self):
        self.rebuild()
        self.assertTrue((self.config.gui_dir / "private" / "graph-data.json").is_file())
        self.assertTrue((self.config.gui_dir / "public" / "graph-data.json").is_file())

    def test_the_counts_come_back(self):
        self.assertEqual(self.rebuild()["map_authors"], 1)

    def test_the_graph_is_held_while_it_is_read(self):
        held = []
        write = rebuild.write_site_data

        def watching(*args, **kwargs):
            held.append(locks.holder(self.db, GRAPH))
            return write(*args, **kwargs)

        with patch.object(rebuild, "write_site_data", watching):
            self.rebuild()
        self.assertIsNotNone(held[0], "граф не был занят во время пересборки")

    def test_the_graph_is_free_again_afterwards(self):
        self.rebuild()
        self.assertIsNone(locks.holder(self.db, GRAPH))

    def test_a_rebuild_waits_for_a_publish(self):
        with locks.held(self.db, GRAPH, "publisher"), self.assertRaises(locks.Busy):
            self.rebuild()


class EmptyGraphTest(unittest.TestCase):
    """A graph with no ITMO authors is an empty map, not a failure.

    cKDTree and fa2_modified both refuse an empty graph, so the layout step
    raised and the rebuild died on a fresh database or a group nobody had published.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.snapshot = self.tmp / "graph_snapshot.json"

    def build(self, db):
        write_snapshot(self.snapshot, db)
        return write_site_data(self.snapshot, self.tmp / "out", seed=42)

    def test_an_empty_graph_builds(self):
        self.assertEqual(self.build(blank())["map_authors"], 0)

    def test_a_publication_with_no_itmo_author_builds(self):
        self.assertEqual(self.build({**blank(), "publications": [publication()]})["map_pubs"], 0)

    def test_departments_without_people_build(self):
        db = {**blank(), "publications": [publication()], "pub_depts": [{"pid": "W1", "did": "D0"}],
              "departments": [{"id": f"D{n}", "name_ru": f"Кафедра {n}", "name_en": ""} for n in range(4)]}
        self.assertEqual(self.build(db)["map_authors"], 0)

    def test_the_files_are_written_even_when_empty(self):
        # scripts/deploy.sh refuses to deploy without all four.
        self.build(blank())
        self.assertEqual(sorted(p.name for p in (self.tmp / "out" / "private").iterdir()), DETAIL_FILES)


if __name__ == "__main__":
    unittest.main()
