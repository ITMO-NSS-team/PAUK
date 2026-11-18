"""Exercise the progress path through the worker and the jobs page."""

import unittest
from unittest.mock import patch

import mongomock
from fastapi.testclient import TestClient

from pauk.admin.app import build
from pauk.admin.auth import create_user
from pauk.jobs import store, worker
from pauk.jobs.models import JobKind, JobState
from pauk.pipeline import enrich
from pauk.pipeline.enrich import Enricher
from pauk.pipeline.stages.base import EnrichmentStage
from pauk.settings import Settings
from pauk.storage import PreparedStore, RawStore


class ProgressChainTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        self.config = Settings(neo4j_password="")
        create_user(self.db, "reader", "hunter2", role="admin")
        self.client = TestClient(build(self.config, self.db), follow_redirects=False)
        response = self.client.post("/login", data={"login": "reader", "password": "hunter2"})
        self.assertEqual(response.status_code, 303)
        self.worker = worker.Worker(config=self.config, db=self.db, name="worker-1", poll_seconds=0)
        self.job = store.enqueue(self.db, JobKind.COLLECT, {"group": "sample", "work_id": "W1"})

    def run_stage(self, after_update, after_second_update=lambda: None):
        test = self

        class ReportingStage(EnrichmentStage):
            name = "reporting-test"
            REPORT_SECONDS = 0

            def run(self):
                with self.progress_bar(total=2, label="repositories") as bar:
                    bar.update()
                    after_update()
                    bar.update()
                    after_second_update()
                test.completed = True
                return {"seen": 2}

        def collect(config, db, payload, stop, report):
            return Enricher(PreparedStore(db, "sample"), RawStore(db, "sample"), config).run(
                on_stage=report)

        with (patch.object(enrich, "ALL_STAGES", (ReportingStage,)),
              patch.dict(worker.STEPS, {JobKind.COLLECT: collect}),
              patch("sys.stderr.isatty", return_value=False)):
            self.assertTrue(self.worker.run_once())

    def test_progress_reaches_jobs_html_during_the_job(self):
        def inspect_first_update():
            saved = store.read(self.db, self.job.id)
            self.assertEqual(saved.state, JobState.RUNNING)
            self.assertEqual(saved.progress["step"], "repositories 1/2")
            self.assertEqual((saved.progress["done"], saved.progress["total"]), (0, 1))
            page = self.client.get("/jobs")
            self.assertEqual(page.status_code, 200)
            self.assertIn("repositories 1/2", page.text)
            self.assertRegex(page.text, r"этап\s+1 из 1")

        def inspect_second_update():
            page = self.client.get("/jobs")
            self.assertEqual(page.status_code, 200)
            self.assertIn("repositories 2/2", page.text)
            self.assertNotIn("repositories 1/2", page.text)

        self.completed = False
        self.run_stage(inspect_first_update, inspect_second_update)
        self.assertTrue(self.completed)
        self.assertEqual(store.read(self.db, self.job.id).state, JobState.DONE)
        self.assertEqual(store.read(self.db, self.job.id).progress["step"], "repositories 2/2")

    def test_cancellation_is_seen_at_next_manual_update(self):
        self.completed = False

        def request_cancel():
            self.assertTrue(store.request_cancel(self.db, self.job.id))

        self.run_stage(request_cancel)
        self.assertFalse(self.completed)
        saved = store.read(self.db, self.job.id)
        self.assertEqual(saved.state, JobState.CANCELLED)
        self.assertEqual(saved.progress["step"], "repositories 2/2")


if __name__ == "__main__":
    unittest.main()
