"""The curated `title,repo_url` import, and what it does with a rename.

Loaded from scripts/ by path: the directory is a collection of operator
tools, not an importable package.
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

import mongomock

from pauk.storage import PreparedStore

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "import_curated_repos.py"
_spec = importlib.util.spec_from_file_location("import_curated_repos", SCRIPT)
import_curated_repos = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = import_curated_repos
_spec.loader.exec_module(import_curated_repos)


def selected_row(owner, name, publication_id="W1"):
    return {"title": f"{owner}/{name}", "owner": owner, "name": name,
            "repo_id": import_curated_repos.repo_id_for(owner, name),
            "publication_id": publication_id, "publication_title": "A paper",
            "match": "exact", "ratio": 1.0}


def payload_for(owner, name, *, repo_id=1):
    """What GitHub answers — with `owner`/`name` already redirected."""
    return {"ok": True, "has_readme": True, "payload": {
        "id": repo_id, "name": name, "html_url": f"https://github.com/{owner}/{name}",
        "owner": {"login": owner, "type": "Organization"},
        "description": "a tool", "stargazers_count": 7}}


def stored(repo_id, url, **extra):
    return {"id": repo_id, "url": url, "name": repo_id.rsplit("_", 1)[-1],
            "cited_urls": [url], "publication_ids": [], **extra}


class RenamedRepositoryTest(unittest.TestCase):
    """A row found under the cited id must not be left behind by the re-key.

    GitHub redirects a renamed repository, so `nccr-itmo/FEDOT` answers as
    `aimclub/FEDOT`. The row is re-keyed to the canonical id; upserting it
    then creates a second document, and the one it was read from stays in
    place holding the same url — the duplicate the canonical keying exists
    to prevent, and one the graph's uniqueness constraint rejects.
    """

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]

    def build(self):
        row = selected_row("nccr-itmo", "FEDOT")
        return import_curated_repos.build_documents(
            self.db, [row], {row["repo_id"]: payload_for("aimclub", "FEDOT")})

    def test_the_row_read_under_the_old_id_is_marked_superseded(self):
        self.db.repositories.insert_one(
            stored("github_nccr-itmo_fedot", "https://github.com/nccr-itmo/FEDOT"))
        documents, failures = self.build()
        self.assertEqual(failures, [])
        self.assertEqual([doc["id"] for doc in documents], ["github_aimclub_fedot"])
        self.assertEqual(documents[0]["superseded_id"], "github_nccr-itmo_fedot")

    def test_the_old_id_travels_in_merged_ids(self):
        # A graph node still keyed by the old id resolves to the survivor
        # through merged_ids; without it the rename orphans the node.
        self.db.repositories.insert_one(
            stored("github_nccr-itmo_fedot", "https://github.com/nccr-itmo/FEDOT"))
        documents, _ = self.build()
        self.assertEqual(documents[0]["document"]["merged_ids"], ["github_nccr-itmo_fedot"])

    def test_a_row_already_under_the_canonical_id_supersedes_nothing(self):
        self.db.repositories.insert_one(
            stored("github_aimclub_fedot", "https://github.com/aimclub/FEDOT"))
        documents, _ = self.build()
        self.assertIsNone(documents[0]["superseded_id"])
        self.assertEqual(documents[0]["document"]["merged_ids"], [])

    def test_a_repository_new_to_the_database_supersedes_nothing(self):
        documents, _ = self.build()
        self.assertTrue(documents[0]["is_new"])
        self.assertIsNone(documents[0]["superseded_id"])

    def test_a_row_never_lists_the_id_it_ends_up_with(self):
        # The old row can already name the canonical id — an earlier fold
        # wrote it there — and would otherwise be merged into itself.
        self.db.repositories.insert_one(
            stored("github_nccr-itmo_fedot", "https://github.com/nccr-itmo/FEDOT",
                   merged_ids=["github_aimclub_fedot"]))
        documents, _ = self.build()
        self.assertEqual(documents[0]["document"]["merged_ids"], ["github_nccr-itmo_fedot"])


class ApplyRetiresSupersededRowsTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        self.collection = self.db[PreparedStore.COLLECTIONS["repositories"]]
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def apply(self, documents, group="curated"):
        plan = Path(self.tmp.name) / "plan.json"
        plan.write_text(json.dumps({"group": group, "documents": documents}), encoding="utf-8")
        import_curated_repos.command_apply(
            Namespace(plan=plan, group=None, yes=True), None, self.db)

    def plan_document(self, superseded_id=None):
        row = selected_row("nccr-itmo", "FEDOT")
        documents, _ = import_curated_repos.build_documents(
            self.db, [row], {row["repo_id"]: payload_for("aimclub", "FEDOT")})
        self.assertEqual(documents[0]["superseded_id"], superseded_id)
        return documents

    def test_the_superseded_document_is_gone_after_apply(self):
        self.collection.insert_one(
            {"_id": "github_nccr-itmo_fedot", "id": "github_nccr-itmo_fedot",
             "url": "https://github.com/nccr-itmo/FEDOT", "name": "FEDOT",
             "cited_urls": ["https://github.com/nccr-itmo/FEDOT"],
             "publication_ids": [], "groups": ["curated"], "_version": 1})
        self.apply(self.plan_document("github_nccr-itmo_fedot"))
        self.assertEqual(sorted(doc["_id"] for doc in self.collection.find({})),
                         ["github_aimclub_fedot"])
        survivor = self.collection.find_one({"_id": "github_aimclub_fedot"})
        self.assertEqual(survivor["merged_ids"], ["github_nccr-itmo_fedot"])
        self.assertEqual(survivor["url"], "https://github.com/aimclub/FEDOT")

    def test_a_plan_that_supersedes_nothing_deletes_nothing(self):
        self.collection.insert_one(
            {"_id": "github_aimclub_fedot", "id": "github_aimclub_fedot",
             "url": "https://github.com/aimclub/FEDOT", "name": "FEDOT",
             "cited_urls": ["https://github.com/aimclub/FEDOT"],
             "publication_ids": [], "groups": ["curated"], "_version": 1})
        self.apply(self.plan_document())
        self.assertEqual([doc["_id"] for doc in self.collection.find({})],
                         ["github_aimclub_fedot"])

    def test_an_id_written_by_the_plan_is_never_deleted_as_superseded(self):
        # Two CSV rows can name a repository under both its old and its new
        # owner; the old id is then both retired by one and written by the
        # other, and deleting it would drop a row the plan just wrote.
        documents = [
            {"id": "github_aimclub_fedot", "is_new": False,
             "superseded_id": "github_nccr-itmo_fedot", "added_publication_ids": [],
             "document": {"id": "github_aimclub_fedot", "name": "FEDOT",
                          "url": "https://github.com/aimclub/FEDOT"}},
            {"id": "github_nccr-itmo_fedot", "is_new": True, "superseded_id": None,
             "added_publication_ids": [],
             "document": {"id": "github_nccr-itmo_fedot", "name": "FEDOT",
                          "url": "https://github.com/nccr-itmo/FEDOT"}},
        ]
        self.apply(documents)
        self.assertEqual(sorted(doc["_id"] for doc in self.collection.find({})),
                         ["github_aimclub_fedot", "github_nccr-itmo_fedot"])


if __name__ == "__main__":
    unittest.main()
