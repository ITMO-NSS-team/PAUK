"""Progress reporting from the real repository and group-dedup stages."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import mongomock

from pauk.jobs.worker import Cancelled
from pauk.models import CodeLink, RepoLink
from pauk.pipeline.stages.dedup import DedupStage
from pauk.pipeline.stages.repositories import RepositoriesStage
from pauk.settings import Settings
from pauk.storage import PreparedStore, RawStore


class ActualStagesProgressTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = mongomock.MongoClient()["pauk_test"]
        self.prepared = PreparedStore(self.db, "sample")
        self.raw = RawStore(self.db, "sample")
        self.config = Settings(data_dir=Path(self.tmp.name), person_resolution_enabled=False)
        self.prepared.write_models("repo_links", [RepoLink(publication_id="W1", links=[
            CodeLink(url="https://github.com/org/one"), CodeLink(url="https://github.com/org/two"),
        ])])

    def run_real_stage(self, stage_class, callback):
        stage = stage_class(self.prepared, self.raw, self.config, on_progress=callback)
        stage.REPORT_SECONDS = 0
        with (patch("sys.stderr.isatty", return_value=False),
              patch("pauk.pipeline.stages.repositories.GitHubClient") as client):
            client.return_value.get_repository.side_effect = lambda owner, name: {
                "html_url": f"https://github.com/{owner}/{name}", "name": name,
                "owner": {"login": owner, "type": "User"},
            }
            client.return_value.has_readme.return_value = True
            client.return_value.contributors.return_value = []
            client.return_value.commits.return_value = []
            return stage.run()

    def test_repositories_reports_each_repository(self):
        received = []
        self.run_real_stage(RepositoriesStage, lambda *args: received.append(args))
        label = RepositoriesStage.progress_label
        self.assertEqual(received, [(label, 1, 2), (label, 2, 2), (label, 2, 2)])

    def test_dedup_reports_each_actual_step(self):
        received = []
        self.run_real_stage(DedupStage, lambda *args: received.append(args))
        self.assertEqual(received, [("Deduplication: publications", 0, 3),
                                    ("Deduplication: publications", 1, 3),
                                    ("Deduplication: repositories", 1, 3),
                                    ("Deduplication: repositories", 2, 3),
                                    ("Deduplication: authors", 2, 3),
                                    ("Deduplication: authors", 3, 3),
                                    ("Deduplication: authors", 3, 3)])

    def test_repositories_flushes_last_progress_when_updates_are_fast(self):
        received = []
        with patch("pauk.pipeline.stages.base.monotonic", side_effect=[10.0, 10.5, 10.6]):
            stage = RepositoriesStage(self.prepared, self.raw, self.config,
                                      on_progress=lambda *args: received.append(args))
            with patch("pauk.pipeline.stages.repositories.GitHubClient") as client:
                client.return_value.get_repository.side_effect = lambda owner, name: {
                    "html_url": f"https://github.com/{owner}/{name}", "name": name,
                    "owner": {"login": owner, "type": "User"},
                }
                client.return_value.has_readme.return_value = True
                client.return_value.contributors.return_value = []
                client.return_value.commits.return_value = []
                stage.run()
        self.assertEqual(received, [(stage.progress_label, 1, 2), (stage.progress_label, 2, 2)])

    def test_dedup_reports_substeps_before_they_start(self):
        received = []
        stage = DedupStage(self.prepared, self.raw, self.config,
                           on_progress=lambda *args: received.append(args))
        def persons():
            self.assertEqual(received[-1], ("Deduplication: authors", 2, 3))
            return 0, 0, 0
        with (patch.object(stage, "_dedup_publications", return_value={}),
              patch.object(stage, "_dedup_repositories", return_value={}),
              patch.object(stage, "_dedup_persons", side_effect=persons),
              patch("pauk.pipeline.stages.base.monotonic", side_effect=[10.0, 10.01, 10.05, 10.1, 10.15, 10.2])):
            stage.run()
        self.assertEqual(received, [("Deduplication: publications", 0, 3),
                                    ("Deduplication: repositories", 1, 3),
                                    ("Deduplication: authors", 2, 3),
                                    ("Deduplication: authors", 3, 3)])

    def test_callback_exceptions_are_not_swallowed(self):
        for stage_class in (RepositoriesStage, DedupStage):
            for exception in (RuntimeError, Cancelled):
                with (self.subTest(stage=stage_class.name, exception=exception.__name__),
                      self.assertRaisesRegex(exception, "stop")):
                    self.run_real_stage(stage_class, Mock(side_effect=exception("stop")))


if __name__ == "__main__":
    unittest.main()
