"""The one-off repair that drops IMPLEMENTS claims left by the old stage.

Loaded from scripts/ by path: the directory is a collection of operator
tools, not an importable package.
"""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mongomock

from pauk.storage import PreparedStore

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "repair_implements.py"
_spec = importlib.util.spec_from_file_location("repair_implements", SCRIPT)
repair_implements = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = repair_implements
_spec.loader.exec_module(repair_implements)


def repo_doc(repo_id, url, *, cited_urls=(), publication_ids=(), groups=("sample",)):
    return {"_id": repo_id, "url": url, "cited_urls": list(cited_urls) or [url],
            "name": repo_id.rsplit("_", 1)[-1], "publication_ids": list(publication_ids),
            "groups": list(groups), "_version": 1}


def links_doc(publication_id, *links):
    return {"_id": publication_id, "publication_id": publication_id,
            "links": [{"url": url, "is_relevant": verdict} for url, verdict in links],
            "groups": ["sample"]}


class IrrelevantClaimsTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        self.repos = self.db[PreparedStore.COLLECTIONS["repositories"]]
        self.links = self.db[PreparedStore.COLLECTIONS["repo_links"]]

    def test_a_link_judged_someone_elses_loses_its_claim(self):
        self.repos.insert_one(repo_doc("github_scikit-learn_scikit-learn",
                                       "https://github.com/scikit-learn/scikit-learn",
                                       publication_ids=["W1"]))
        self.links.insert_one(links_doc("W1", ("https://github.com/scikit-learn/scikit-learn", False)))
        self.assertEqual(repair_implements.irrelevant_claims(self.db),
                         {"github_scikit-learn_scikit-learn": {"W1"}})

    def test_one_relevant_link_keeps_the_claim(self):
        self.repos.insert_one(repo_doc("github_lab_tool", "https://github.com/lab/tool",
                                       publication_ids=["W1"]))
        self.links.insert_one(links_doc(
            "W1", ("https://github.com/lab/tool", False), ("https://github.com/lab/tool", True)))
        self.assertEqual(repair_implements.irrelevant_claims(self.db)["github_lab_tool"], set())

    def test_a_renamed_repository_is_found_by_the_url_it_was_cited_as(self):
        # The stage re-keys the row to the name GitHub redirects to, so the
        # id derivable from the cited URL matches no row at all; without the
        # cited_urls lookup the stale claim would survive the repair.
        self.repos.insert_one(repo_doc(
            "github_lab_new-name", "https://github.com/lab/new-name",
            cited_urls=["https://github.com/lab/old-name", "https://github.com/lab/new-name"],
            publication_ids=["W1"]))
        self.links.insert_one(links_doc("W1", ("https://github.com/Lab/old-name/", False)))
        self.assertEqual(repair_implements.irrelevant_claims(self.db),
                         {"github_lab_new-name": {"W1"}})


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        self.repos = self.db[PreparedStore.COLLECTIONS["repositories"]]
        self.links = self.db[PreparedStore.COLLECTIONS["repo_links"]]
        self.links.insert_one(links_doc("W1", ("https://github.com/lab/tool", False)))

    def run_script(self):
        """The script with --apply, against this test's in-memory database."""
        report = Path(self.enterContext(tempfile.TemporaryDirectory())) / "report.json"
        argv = ["repair_implements.py", "--apply", "--report", str(report)]
        settings = type(repair_implements.settings)(mongo_db=self.db.name)
        with patch.object(sys, "argv", argv), \
             patch.object(repair_implements, "settings", settings), \
             patch.object(repair_implements, "get_mongo_client", lambda *_: self.db.client):
            self.assertEqual(repair_implements.main(), 0)
        return report

    def test_the_claim_is_removed_from_the_stored_row(self):
        self.repos.insert_one(repo_doc("github_lab_tool", "https://github.com/lab/tool",
                                       publication_ids=["W1", "W2"]))
        self.run_script()
        stored = self.repos.find_one({"_id": "github_lab_tool"})
        self.assertEqual(stored["publication_ids"], ["W2"])
        self.assertEqual(stored["groups"], ["sample"])

    def test_a_repository_belonging_to_no_group_is_left_alone(self):
        # upsert_models tags whatever group it is handed, so a placeholder
        # would be written into `groups` as if it were a real one.
        self.repos.insert_one(repo_doc("github_lab_tool", "https://github.com/lab/tool",
                                       publication_ids=["W1"], groups=[]))
        self.run_script()
        stored = self.repos.find_one({"_id": "github_lab_tool"})
        self.assertEqual(stored["publication_ids"], ["W1"])
        self.assertEqual(stored.get("groups") or [], [])


if __name__ == "__main__":
    unittest.main()
