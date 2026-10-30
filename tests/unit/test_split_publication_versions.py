"""The repair that takes apart publications folded on a title under two DOIs.

Loaded from scripts/ by path: the directory is a collection of operator
tools, not an importable package.
"""

import importlib.util
import json
import sys
import unittest
from pathlib import Path

import mongomock

from tests.bench.mocks import RecordingNeo4jClient

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "split_publication_versions.py"
_spec = importlib.util.spec_from_file_location("split_publication_versions", SCRIPT)
split = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = split
_spec.loader.exec_module(split)

TITLE = "Deep learning for spider web classification"


def entry(work_id, doi, *authors, journal=None, title=TITLE):
    return {"openalex_id": work_id, "title": title, "doi": doi, "journal": journal,
            "authors": [{"person_id": person} for person in authors]}


class PlanSplitsTest(unittest.TestCase):
    canonical = {"id": "W2", "doi": "10.3390/s1", "type": "article", "title": TITLE}

    def plan(self, *entries, raw=None):
        raw = raw or {}
        return split.plan_splits(self.canonical, [entry("W2", "10.3390/s1", "P1"), *entries],
                                 [e["openalex_id"] for e in entries], lambda work_id: raw.get(work_id))

    def test_a_second_doi_with_a_bucket_is_taken_apart(self):
        (found,) = self.plan(entry("W1", "10.48550/arxiv.1", "P1", "P2"),
                             raw={"W1": ({"type": "preprint"}, "q1"), "W2": ({}, "q2")})
        self.assertEqual((found["id"], found["bucket"]), ("W1", "MISSED_BY_NORM"))
        self.assertEqual(found["lone_authors"], ["P2"])
        self.assertEqual(found["groups"], ["q1", "q2"])

    def test_same_doi_or_no_doi_stays_folded(self):
        self.assertEqual(self.plan(entry("W1", "https://doi.org/10.3390/S1"), entry("W3", None)), [])

    def test_a_pair_the_rules_drop_stays_folded(self):
        self.assertEqual(self.plan(entry("W1", "10.1/x", title="Introduction"),
                                   raw={"W1": ({}, "q1")}), [])

    def test_a_record_without_raw_stays_folded(self):
        # Its ledger entry is the last copy: nothing could rebuild it.
        self.assertEqual(self.plan(entry("W1", "10.48550/arxiv.1", "P1", "P2")), [])

    def test_without_the_survivors_own_entry_no_authorship_is_taken(self):
        (found,) = split.plan_splits(
            {**self.canonical, "authors": [{"person_id": "P1"}, {"person_id": "P2"}]},
            [entry("W1", "10.48550/arxiv.1", "P1", "P2")], ["W1"],
            lambda work_id: ({"type": "preprint"}, "q1"))
        self.assertEqual(found["lone_authors"], [])
        self.assertFalse(found["survivor_authors_known"])


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().db
        self.found = {"survivor": "W2", "id": "W1", "bucket": "MISSED_BY_NORM",
                      "lone_authors": ["P2"], "survivor_authors_known": True, "groups": ["q1"]}

    def test_mongo_survivor_and_lone_authors_let_go(self):
        self.db.publications.insert_one({"_id": "W2", "merged_ids": ["W1", "W9"],
                                         "versions": [entry("W1", "10.1/a"), entry("W2", "10.1/b")]})
        self.db.persons.insert_one({"_id": "P2", "authored": [{"publication_id": "W2"},
                                                              {"publication_id": "W5"}]})
        split.apply_mongo(self.db, [self.found])
        survivor = self.db.publications.find_one({"_id": "W2"})
        self.assertEqual(survivor["merged_ids"], ["W9"])
        self.assertEqual([v["openalex_id"] for v in survivor["versions"]], ["W2"])
        self.assertEqual(self.db.persons.find_one({"_id": "P2"})["authored"], [{"publication_id": "W5"}])

    def test_graph_survivor_no_longer_maps_the_record(self):
        client = RecordingNeo4jClient()
        client.nodes["Publication"] = {"W2": {
            "title": TITLE, "merged_ids": ["W1"],
            "versions": json.dumps([entry("W1", "10.1/a"), entry("W2", "10.1/b")])}}
        split.apply_graph(client, [self.found])
        self.assertEqual(client.fetch_merged_id_map("Publication"), {})
        versions = json.loads(client.nodes["Publication"]["W2"]["versions"])
        self.assertEqual([v["openalex_id"] for v in versions], ["W2"])


if __name__ == "__main__":
    unittest.main()
