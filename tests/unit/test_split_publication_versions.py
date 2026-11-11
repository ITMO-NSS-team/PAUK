"""The repair that takes apart publications folded on a title under two DOIs.

Loaded from scripts/ by path: the directory is a collection of operator
tools, not an importable package.
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mongomock
import pymupdf

from pauk.graph.overrides import LINK, record_relationship_override
from pauk.models import CodeLink, Person, Publication, RepoLink, Repository
from pauk.models.processing import ProcessingState, ProcessingStatus
from pauk.models.relations import Authorship
from pauk.settings import Settings
from pauk.storage import PreparedStore, RawStore
from pauk.storage.pdf import PdfStore
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
    preprint = "https://github.com/lab/preprint"
    article = "https://github.com/lab/article"
    shared = "https://github.com/lab/shared"
    curated = "https://github.com/lab/curated"

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.report_path = Path(folder.name) / "report.json"
        static = self.report_path.parent / "static"
        static.mkdir()
        (static / "departments.jsonl").write_text("\n".join(json.dumps({
            "id": f"dept-{number}", "name_en": f"Affiliation of A{number}",
        }) for number in (1, 2, 9)))
        self.mongo = mongomock.MongoClient()
        self.db = self.mongo.db
        self.graph = RecordingNeo4jClient()
        self.raw = RawStore(self.db, "old")
        self.prepared = PreparedStore(self.db, "old")
        self.report = {"mongo": [{"survivor": "W2", "id": "W1"}],
                       "graph": [{"survivor": "W2", "id": "W1"}]}

    def add_work(self, work_id, doi, author, links, *, preprint=False):
        url = f"https://example.org/{work_id}.pdf"
        kind = "preprint" if preprint else "article"
        self.raw.append("openalex_works", {
            "id": f"https://openalex.org/{work_id}", "title": TITLE,
            "doi": doi, "type": kind, "publication_date": "2024-01-01",
            "primary_location": {"pdf_url": url},
            "locations": [{"pdf_url": url}],
            "authorships": [{
                "author": {"id": f"https://openalex.org/{author}", "display_name": f"Author {author}"},
                "raw_affiliation_strings": [f"Affiliation of {author}"],
                "institutions": [{"ror": "https://ror.org/04txgxn49"}],
            }],
        }, {})
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            page.insert_text((60, 60), "\n".join(links) or "No code links here")
            return url, pdf.tobytes()

    def seed(self, already_split):
        pdfs = dict([
            self.add_work("W1", "10.48550/arxiv.1", "A1", [self.preprint, self.shared], preprint=True),
            self.add_work("W2", "10.3390/s1", "A2", [self.article, self.shared]),
            self.add_work("W9", "10.3390/s1", "A9", []),
        ])
        pub = Publication(id="W2", title=TITLE, doi="10.3390/s1", has_code=True, code_url=self.preprint,
                          pdf_urls=["https://example.org/W1.pdf"], merged_ids=["W9"] if already_split else ["W1", "W9"])
        self.prepared.upsert_models("publications", [pub])
        self.prepared.upsert_models("repo_links", [RepoLink(publication_id="W2", links=[
            CodeLink(url=self.preprint, is_relevant=True), CodeLink(url=self.shared, is_relevant=True),
        ])])
        self.prepared.upsert_models("persons", [Person(id="A1", is_itmo=False, authored=[
            Authorship(publication_id="W2", is_corresponding=True), Authorship(publication_id="W7"),
        ])])
        for url in (self.preprint, self.article, self.shared, self.curated):
            name = url.rsplit("/", 1)[-1]
            repo_id = f"github_lab_{name}"
            claims = ["W2", "W7"] if url == self.preprint else ["W2"]
            self.prepared.upsert_models("repositories", [Repository(
                id=repo_id, url=url, name=name, publication_ids=claims,
                processing={"repositories": ProcessingState(status=ProcessingStatus.COMPLETED)},
            )])
            self.graph.nodes["Repository"][repo_id] = {"url": url}
        self.graph.nodes["Publication"]["W2"] = {
            "title": TITLE, "doi": "10.3390/s1", "code_url": self.preprint, "merged_ids": pub.merged_ids,
        }
        if already_split:
            self.prepared.upsert_models("repo_links", [RepoLink(publication_id="W2", links=[])])
            self.prepared.upsert_models("publications", [pub.model_copy(update={"code_url": None})])
        self.graph.nodes["Publication"]["W3"] = {"title": "A separate work"}
        self.graph.edges["Publication", "IS_VERSION", "Publication", "W2", "W3"] = {}
        record_relationship_override(self.db, "Publication", "IS_VERSION", "Publication",
                                     "W1", "W3", LINK)
        self.graph.nodes["Person"]["A1"] = {"is_itmo": False}
        self.graph.edges["Person", "AUTHORED", "Publication", "A1", "W2"] = {"is_corresponding": True}
        self.graph.edges["Repository", "IMPLEMENTS", "Publication", "github_lab_preprint", "W2"] = {}
        self.graph.edges["Publication", "MENTIONS_LINK", "Repository", "W2", self.preprint] = {
            "pdf_id": ["W1"],
        }
        self.graph.edges["Publication", "MENTIONS_LINK", "Repository", "W2", self.curated] = {}
        record_relationship_override(self.db, "Publication", "MENTIONS_LINK", "Repository",
                                     "W2", self.curated, LINK)
        return pdfs

    def repair(self, *, already_split, fail_graph=False):
        pdfs = self.seed(already_split)
        config = Settings(data_dir=self.report_path.parent, pdf_crawler_url="")
        cache = PdfStore(self.db, config.pdf_dir)
        cache.save("W2", pdfs["https://example.org/W1.pdf"])
        with patch.object(split, "settings", config), patch(
            "pauk.pipeline.stages.code_links.HttpClient"
        ) as http, patch("pauk.pipeline.stages.link_relevance.OpenRouterClient") as llm:
            http.return_value.get_bytes.side_effect = lambda url, **kwargs: pdfs[url]
            llm.return_value.chat_json.return_value = {"is_authors_artifact": True, "confidence": 0.99}
            llm.return_value.last_response = None
            llm.return_value.last_usage = None
            if fail_graph:
                with patch.object(split, "apply_graph_repair", side_effect=RuntimeError("Neo4j down")):
                    self.assertEqual(split._repair(self.mongo, self.db, self.graph, self.report, self.report_path), 1)
                self.assertEqual(self.report["progress"]["W2"]["status"], "prepared")
                self.report = json.loads(self.report_path.read_text())
            self.assertEqual(split._repair(self.mongo, self.db, self.graph, self.report, self.report_path), 0)
            self.assertEqual(http.return_value.get_bytes.call_count, 3)
        self.assertEqual(self.report["progress"]["W2"]["status"], "done")
        self.assertEqual(cache.read("W2"), pdfs["https://example.org/W2.pdf"])

    def assert_rebuilt(self):
        article = self.db.publications.find_one("W2")
        self.assertEqual(article["merged_ids"], ["W9"])
        self.assertEqual(set(article["department_ids"]), {"dept-2", "dept-9"})
        self.assertIn(("Publication", "PRODUCED_BY", "Department", "W1", "dept-1"), self.graph.edges)
        self.assertEqual(set(json.loads(article["code_url"])), {self.article, self.shared})
        self.assertNotIn(self.preprint, article["full_text"])
        self.assertEqual(set(self.db.repositories.find_one("github_lab_preprint")["publication_ids"]), {"W1", "W7"})
        self.assertEqual(set(self.db.repositories.find_one("github_lab_shared")["publication_ids"]), {"W1", "W2"})
        self.assertEqual(self.db.repositories.find_one("github_lab_curated")["publication_ids"], ["W2"])
        self.assertEqual({a["publication_id"] for a in self.db.persons.find_one("A1")["authored"]}, {"W1", "W7"})
        self.assertEqual(self.db.persons.find_one("A9")["authored"][0]["publication_id"], "W2")
        self.assertNotIn(("Person", "AUTHORED", "Publication", "A1", "W2"), self.graph.edges)
        self.assertNotIn(("Repository", "IMPLEMENTS", "Publication", "github_lab_preprint", "W2"), self.graph.edges)
        self.assertIn(("Publication", "MENTIONS_LINK", "Repository", "W2", self.curated), self.graph.edges)
        self.assertIn(("Publication", "IS_VERSION", "Publication", "W1", "W3"), self.graph.edges)
        self.assertNotIn(("Publication", "IS_VERSION", "Publication", "W2", "W3"), self.graph.edges)
        article_mention = self.graph.edges["Publication", "MENTIONS_LINK", "Repository", "W2", self.article]
        self.assertEqual(article_mention["pdf_id"], ["W2"])
        self.assertEqual(self.graph.fetch_canonical_id("Publication", "W9"), "W2")
        self.assertIsNone(self.graph.fetch_canonical_id("Publication", "W1"))

    def test_rebuilds_mongo_and_graph_from_version_specific_sources(self):
        self.repair(already_split=False)
        self.assert_rebuilt()

    def test_missing_source_keeps_the_original_fold_and_repository_claim(self):
        self.seed(already_split=False)
        self.db.raw.delete_one({"payload.id": "https://openalex.org/W9"})
        self.assertEqual(split._repair(self.mongo, self.db, self.graph, self.report, self.report_path), 1)
        self.assertIn("W9", self.report["progress"]["W2"]["error"])
        self.assertEqual(self.db.publications.find_one("W2")["merged_ids"], ["W1", "W9"])
        self.assertEqual(self.db.repositories.find_one("github_lab_preprint")["publication_ids"], ["W2", "W7"])

    def test_resumes_after_mongo_and_repairs_already_detached_rows(self):
        self.repair(already_split=True, fail_graph=True)
        self.assert_rebuilt()
        before = self.db.publications.find_one("W2")["_version"]
        self.assertEqual(split._repair(self.mongo, self.db, self.graph, self.report, self.report_path), 0)
        self.assertEqual(self.db.publications.find_one("W2")["_version"], before)


if __name__ == "__main__":
    unittest.main()
