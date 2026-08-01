import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pauk.models import Publication, RepoLink
from pauk.models.processing import ProcessingStatus
from pauk.pipeline.stages.code_links import CodeLinksStage
from pauk.pipeline.stages.base import PreparedSelection
from pauk.pipeline.stages.repositories import RepositoriesStage
from pauk.storage import PreparedStore, RawStore


class StagesTest(unittest.TestCase):
    def test_code_links_marks_empty_and_found_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepared = PreparedStore(root / "prepared", "sample")
            raw = RawStore(root / "raw", "sample")
            prepared.write_models("publications", [
                Publication(id="W1", title="with code", abstract="https://github.com/org/repo"),
                Publication(id="W2", title="without code"),
            ])
            CodeLinksStage(prepared, raw).run()
            rows = {row.id: row for row in prepared.read_models("publications", Publication)}
            self.assertEqual(rows["W1"].processing["code_links"].status, ProcessingStatus.COMPLETED)
            self.assertEqual(rows["W2"].processing["code_links"].status, ProcessingStatus.COMPLETED_EMPTY)
            self.assertTrue(rows["W1"].has_code)

    def test_code_links_strips_sentence_ending_period_from_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepared = PreparedStore(root / "prepared", "sample")
            raw = RawStore(root / "raw", "sample")
            prepared.write_models("publications", [
                Publication(id="W1", title="t", abstract="Code is available at https://github.com/org/repo."),
            ])
            CodeLinksStage(prepared, raw).run()
            links = [link for row in prepared.read_models("repo_links", RepoLink) for link in row.links]
            self.assertEqual([link.url for link in links], ["https://github.com/org/repo"])

    def test_code_links_canonicalizes_www_github_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepared = PreparedStore(root / "prepared", "sample")
            raw = RawStore(root / "raw", "sample")
            prepared.write_models("publications", [
                Publication(id="W1", title="t", abstract="https://www.github.com/org/repo"),
            ])
            CodeLinksStage(prepared, raw).run()
            links = [link for row in prepared.read_models("repo_links", RepoLink) for link in row.links]
            self.assertEqual([link.url for link in links], ["https://github.com/org/repo"])

    @patch("pauk.pipeline.stages.repositories.GitHubClient")
    def test_forced_repository_enrichment_fetches_each_repository_once(self, github_client):
        github_client.return_value.get_repository.return_value = {
            "html_url": "https://github.com/org/repo", "name": "repo", "owner": {"login": "org"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepared = PreparedStore(root / "prepared", "sample")
            raw = RawStore(root / "raw", "sample")
            prepared.write_models("repo_links", [
                RepoLink(publication_id="W1", links=[{"url": "https://github.com/org/repo"}]),
                RepoLink(publication_id="W2", links=[{"url": "https://www.github.com/org/repo"}]),
            ])
            RepositoriesStage(prepared, raw, force=True).run()
            self.assertEqual(github_client.return_value.get_repository.call_count, 1)

    def test_force_reprocesses_completed_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepared = PreparedStore(root / "prepared", "sample")
            raw = RawStore(root / "raw", "sample")
            prepared.write_models("publications", [
                Publication(id="W1", title="with code", abstract="https://github.com/org/repo"),
            ])
            CodeLinksStage(prepared, raw).run()
            result = CodeLinksStage(prepared, raw).run()
            self.assertEqual(result["publications"], 0)  # completed rows are skipped
            result = CodeLinksStage(prepared, raw, force=True).run()
            self.assertEqual(result["publications"], 1)
            rows = {row.id: row for row in prepared.read_models("publications", Publication)}
            self.assertEqual(rows["W1"].processing["code_links"].attempts, 2)

    def test_code_links_respects_publication_input_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepared = PreparedStore(root / "prepared", "sample")
            raw = RawStore(root / "raw", "sample")
            prepared.write_models("publications", [
                Publication(id="W1", title="selected", abstract="https://github.com/org/repo"),
                Publication(id="W2", title="not selected", abstract="https://github.com/org/other"),
            ])
            CodeLinksStage(prepared, raw, selection=PreparedSelection("publications", frozenset({"W1"}))).run()
            rows = {row.id: row for row in prepared.read_models("publications", Publication)}
            self.assertIn("code_links", rows["W1"].processing)
            self.assertNotIn("code_links", rows["W2"].processing)


if __name__ == "__main__":
    unittest.main()
