import threading
import unittest
from datetime import timedelta
from unittest.mock import patch

import mongomock

from pauk.jobs import store, worker
from pauk.jobs.models import JobKind, now
from pauk.settings import Settings


class WorkerPresenceHeartbeatTest(unittest.TestCase):
    def test_running_job_refreshes_custom_worker_and_removes_it_on_exit(self):
        db = mongomock.MongoClient()["pauk_test"]
        instance = worker.Worker(Settings(), db, name="custom-worker")
        store.enqueue(db, JobKind.PUBLISH, {"group": "2024"})
        running = threading.Event()
        finish = threading.Event()
        refreshed = threading.Event()
        original = store.mark_present
        calls = []

        def mark_present(database, name):
            original(database, name)
            calls.append(name)
            if len(calls) > 1:
                refreshed.set()

        def step(config, database, payload, stop, report):
            database[store.WORKERS].update_one(
                {"_id": instance.name},
                {"$set": {"seen_at": now() - timedelta(minutes=1)}})
            running.set()
            finish.wait(timeout=5)
            instance.stop()
            return {}

        with patch.object(worker, "BEAT_SECONDS", 0.01), \
                patch.object(store, "mark_present", mark_present), \
                patch.dict(worker.STEPS, {JobKind.PUBLISH: step}):
            thread = threading.Thread(target=instance.run_forever)
            thread.start()
            try:
                self.assertTrue(running.wait(timeout=2))
                self.assertTrue(refreshed.wait(timeout=2))
                self.assertEqual(store.workers_present(db), ["custom-worker"])
                self.assertTrue(all(name == "custom-worker" for name in calls))
            finally:
                finish.set()
                instance.stop()
                thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(store.workers_present(db), [])
            self.assertEqual(db[store.WORKERS].count_documents({}), 0)

    def test_beat_interval_is_inside_presence_window(self):
        self.assertLess(worker.BEAT_SECONDS, store.PRESENT_SECONDS)
