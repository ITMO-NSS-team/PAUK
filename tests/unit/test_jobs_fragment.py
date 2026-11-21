import re
import unittest
from unittest.mock import patch

import mongomock
from fastapi.testclient import TestClient

from pauk.admin.app import build
from pauk.admin.auth import SESSIONS, create_user
from pauk.jobs import store
from pauk.jobs.models import JobKind
from pauk.settings import Settings


class JobsFragmentTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "reader", "hunter2", role="admin")
        self.client = TestClient(build(Settings(neo4j_password=""), self.db), follow_redirects=False)
        self.client.post("/login", data={"login": "reader", "password": "hunter2"})

    def test_requires_authentication(self):
        client = TestClient(build(Settings(neo4j_password=""), self.db), follow_redirects=False)
        self.assertEqual(client.get("/jobs/fragment", headers={"accept": "application/json"}).status_code, 401)

    def test_session_removed_after_page_load(self):
        self.assertEqual(self.client.get("/jobs").status_code, 200)
        self.db[SESSIONS].delete_many({})
        response = self.client.get("/jobs/fragment", headers={"accept": "application/json"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "sign in first")
        browser = self.client.get("/jobs/fragment", headers={"accept": "text/html"})
        self.assertEqual(browser.status_code, 303)
        self.assertTrue(browser.headers["location"].startswith("/login?next="))

    def test_jobs_page_script_is_served(self):
        page = self.client.get("/jobs")
        match = re.search(r'<script\s+src="([^"]*jobs\.js)"', page.text)
        self.assertIsNotNone(match)
        script = self.client.get(match.group(1))
        self.assertEqual(script.status_code, 200)
        self.assertIn("createJobsPoller", script.text)

    def test_empty_fragment_uses_same_content_as_page(self):
        response = self.client.get("/jobs/fragment")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("Таких задач ещё не было", response.text)
        self.assertIn("Таких задач ещё не было", self.client.get("/jobs").text)
        self.assertNotIn("<html", response.text)
        self.assertIn('data-active="false"', response.text)

    def test_filtered_history_and_csrf_forms(self):
        first = store.enqueue(self.db, JobKind.COLLECT, {"group": "sample", "work_id": "W1"}, actor="user:first")
        store.enqueue(self.db, JobKind.PUBLISH, {"group": "other"}, actor="user:second")
        response = self.client.get("/jobs/fragment", params={"kind": "collect", "actor": "user:first", "state": "queued"})
        self.assertIn(first.id, response.text)
        self.assertNotIn("user:second", response.text)
        self.assertIn('action="/jobs/cancel"', response.text)
        self.assertIn('name="csrf"', response.text)
        self.assertIn('data-active="true"', response.text)

    def test_running_progress_matches_the_full_page(self):
        job = store.enqueue(self.db, JobKind.COLLECT, {"group": "sample", "work_id": "W1"})
        store.claim(self.db, "worker-1")
        store.start(self.db, job.id)
        store.progress(self.db, job.id, "repositories 1/2", 0, 1)
        fragment = self.client.get("/jobs/fragment").text
        page = self.client.get("/jobs").text
        for body in (fragment, page):
            self.assertIn("repositories 1/2", body)
            self.assertIn('action="/jobs/cancel"', body)
            self.assertIn(job.id, body)

    def test_graph_warning_tracks_running_job_without_extra_queries(self):
        job = store.enqueue(self.db, JobKind.PUBLISH, {"group": "sample"})
        store.claim(self.db, "worker-1")
        store.start(self.db, job.id)
        with patch.object(store, "running", wraps=store.running) as running:
            fragment = self.client.get("/jobs/fragment").text
            self.assertEqual(running.call_count, 2)
        page = self.client.get("/jobs").text
        for body in (fragment, page):
            self.assertIn('id="jobs-graph-banner"', body)
            self.assertIn('class="note bad"', body)
            self.assertIn('href="/jobs"', body)
        store.finish(self.db, job.id, {})
        fragment = self.client.get("/jobs/fragment").text
        self.assertIn('id="jobs-graph-banner"', fragment)
        self.assertNotIn('class="note bad"', fragment)

    def test_pagination_and_versions(self):
        for number in range(store.PAGE + 1):
            store.enqueue(self.db, JobKind.VERSIONS, {}, actor=f"user:{number}")
        response = self.client.get("/jobs/fragment", params={"kind": "versions", "page": 2})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Страница 2 из 2", response.text)
        self.assertIn("версии публикаций", response.text)
        self.assertIn("kind=versions", response.text)


if __name__ == "__main__":
    unittest.main()
