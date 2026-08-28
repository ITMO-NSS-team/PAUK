import threading
import unittest
from unittest.mock import patch

import mongomock

from pauk.jobs import locks, store, worker
from pauk.jobs.models import GRAPH, PAYLOADS, JobKind, JobState, PublishPayload
from pauk.settings import Settings


class WorkerTest(unittest.TestCase):
    """One process taking jobs off the queue, one at a time.

    Every step is replaced here. A worker test that reached a real Neo4j
    would publish a group as a side effect of running the suite — which is
    exactly what happened once while checking this by hand.
    """

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        self.config = Settings(neo4j_uri="bolt://127.0.0.1:7699")
        self.worker = worker.Worker(config=self.config, db=self.db,
                                    name="worker-1", poll_seconds=0)
        self.seen = []

    def step(self, result=None, raises=None):
        """A stand-in for the work, recording what it was handed."""
        def run(config, db, payload):
            self.seen.append(payload)
            if raises is not None:
                raise raises
            return result or {}
        return run

    def queue(self, kind=JobKind.PUBLISH, payload=None, actor="user:roman"):
        return store.enqueue(self.db, kind, payload or {"group": "2024"}, actor=actor)

    def run_once(self, kind=JobKind.PUBLISH, **step):
        with patch.dict(worker.STEPS, {kind: self.step(**step)}):
            return self.worker.run_once()

    def test_an_empty_queue_is_nothing_to_do(self):
        self.assertFalse(self.worker.run_once())

    def test_a_job_is_performed(self):
        job = self.queue()
        self.assertTrue(self.run_once(result={"rows_persons": 7}))
        self.assertEqual(store.read(self.db, job.id).state, JobState.DONE)

    def test_the_result_is_kept(self):
        job = self.queue()
        self.run_once(result={"rows_persons": 7})
        self.assertEqual(store.read(self.db, job.id).result, {"rows_persons": 7})

    def test_the_worker_signs_the_job(self):
        job = self.queue()
        self.run_once()
        self.assertEqual(store.read(self.db, job.id).worker, "worker-1")

    def test_the_step_is_handed_a_model_not_a_dict(self):
        # The payload is checked on the way out of the queue, so a step
        # reads named fields rather than guessing at a mapping.
        self.queue()
        self.run_once()
        self.assertIsInstance(self.seen[0], PublishPayload)
        self.assertEqual(self.seen[0].group, "2024")

    def test_a_failing_job_is_recorded_not_raised(self):
        job = self.queue()
        self.assertTrue(self.run_once(raises=RuntimeError("boom")))
        stored = store.read(self.db, job.id)
        self.assertEqual(stored.state, JobState.FAILED)
        self.assertEqual(stored.error, "RuntimeError: boom")

    def test_the_worker_survives_a_failing_job(self):
        self.queue()
        self.run_once(raises=RuntimeError("boom"))
        second = self.queue()
        self.run_once(result={"rows_persons": 1})
        self.assertEqual(store.read(self.db, second.id).state, JobState.DONE)

    def test_a_busy_resource_puts_the_job_back(self):
        job = self.queue()
        self.run_once(raises=locks.Busy("graph is busy"))
        stored = store.read(self.db, job.id)
        self.assertEqual(stored.state, JobState.QUEUED)
        self.assertIsNone(stored.worker)

    def test_a_busy_resource_is_not_a_failure(self):
        job = self.queue()
        self.run_once(raises=locks.Busy("graph is busy"))
        self.assertIsNone(store.read(self.db, job.id).error)

    def test_a_busy_resource_makes_the_worker_wait(self):
        # False means "nothing got done", which is what stops the loop from
        # spinning on the same job while somebody else holds the graph.
        self.queue()
        self.assertFalse(self.run_once(raises=locks.Busy("graph is busy")))

    def test_a_job_cancelled_in_the_queue_is_never_claimed(self):
        job = self.queue()
        store.request_cancel(self.db, job.id)
        with patch.dict(worker.STEPS, {JobKind.PUBLISH: self.step()}):
            self.assertFalse(self.worker.run_once())
        self.assertEqual(self.seen, [])
        self.assertEqual(store.read(self.db, job.id).state, JobState.CANCELLED)

    def test_a_job_cancelled_while_it_waited_is_not_started_again(self):
        """The only way a claimed job arrives already cancelled.

        Asked to stop while it was running, then handed back because the
        resource was busy — `requeue` keeps the request, so the next worker
        to pick it up has to honour it rather than start the run.
        """
        job = self.queue()

        def cancel_then_wait(config, db, payload):
            store.request_cancel(db, job.id)
            raise locks.Busy("graph is busy")

        with patch.dict(worker.STEPS, {JobKind.PUBLISH: cancel_then_wait}):
            self.worker.run_once()
        self.assertEqual(store.read(self.db, job.id).state, JobState.QUEUED)
        self.assertTrue(store.read(self.db, job.id).cancel_requested)

        with patch.dict(worker.STEPS, {JobKind.PUBLISH: self.step()}):
            self.assertTrue(self.worker.run_once())
        self.assertEqual(self.seen, [])
        self.assertEqual(store.read(self.db, job.id).state, JobState.CANCELLED)

    def test_a_job_settled_underneath_is_not_run(self):
        job = self.queue()
        original = store.claim

        def settle_then_claim(db, name):
            claimed = original(db, name)
            store.fail(db, job.id, "settled elsewhere")
            return claimed

        with patch.object(store, "claim", settle_then_claim), \
                patch.dict(worker.STEPS, {JobKind.PUBLISH: self.step()}):
            self.worker.run_once()
        self.assertEqual(self.seen, [])
        self.assertEqual(store.read(self.db, job.id).state, JobState.FAILED)


class DispatchTest(unittest.TestCase):
    """The table is closed, and it is the only way a job names its work."""

    def test_every_kind_has_a_step(self):
        # A kind added without a step would be claimed, started and then
        # raise KeyError — a job that fails for a reason nobody can read.
        self.assertEqual(set(worker.STEPS), set(JobKind))

    def test_every_kind_has_a_payload_model(self):
        self.assertEqual(set(PAYLOADS), set(JobKind))

    def test_a_step_is_looked_up_by_the_enum(self):
        # Not by a string off the document: a job must not be able to name
        # a callable of its own.
        for kind in worker.STEPS:
            with self.subTest(kind=kind):
                self.assertIsInstance(kind, JobKind)


class HeartbeatTest(unittest.TestCase):
    """A long run has to keep saying it is alive.

    The work is one synchronous call that does not come back for hours, so
    nothing renews the lease from inside it.
    """

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        self.config = Settings(neo4j_uri="bolt://127.0.0.1:7699")
        self.worker = worker.Worker(config=self.config, db=self.db,
                                    name="worker-1", poll_seconds=0)

    def run_with_beat(self, body):
        beaten = threading.Event()

        def step(config, db, payload):
            beaten.wait(timeout=2)
            return {}

        with patch.object(worker, "BEAT_SECONDS", 0.01), \
                patch.dict(worker.STEPS, {JobKind.PUBLISH: step}):
            thread = threading.Thread(target=self.worker.run_once)
            thread.start()
            try:
                body()
            finally:
                beaten.set()
                thread.join(timeout=5)

    def test_the_job_says_it_is_alive(self):
        job = store.enqueue(self.db, JobKind.PUBLISH, {"group": "2024"})
        seen = []

        def watch():
            for _ in range(200):
                stored = store.read(self.db, job.id)
                if stored.state == JobState.RUNNING and stored.heartbeat_at is not None:
                    seen.append(stored.heartbeat_at)
                    if len(seen) >= 2 and seen[-1] != seen[0]:
                        return
                threading.Event().wait(0.01)

        self.run_with_beat(watch)
        self.assertGreaterEqual(len(seen), 2, "сердцебиение не обновлялось")

    def test_the_lease_is_pushed_out(self):
        store.enqueue(self.db, JobKind.PUBLISH, {"group": "2024"})
        locks.acquire(self.db, GRAPH, "worker-1")
        first = self.db[locks.COLLECTION].find_one({"_id": GRAPH})["expires_at"]
        seen = []

        def watch():
            for _ in range(200):
                row = self.db[locks.COLLECTION].find_one({"_id": GRAPH})
                if row and row["expires_at"] != first:
                    seen.append(row["expires_at"])
                    return
                threading.Event().wait(0.01)

        self.run_with_beat(watch)
        self.assertTrue(seen, "срок замка не продлевался")

    def test_the_beat_stops_with_the_job(self):
        store.enqueue(self.db, JobKind.PUBLISH, {"group": "2024"})
        self.run_with_beat(lambda: None)
        alive = [thread for thread in threading.enumerate() if thread.name.startswith("beat-")]
        self.assertEqual(alive, [])


class StopTest(unittest.TestCase):
    """Asked to stop, not interrupted: a publish cut in half is the one
    ordering this project is careful about everywhere else."""

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        self.worker = worker.Worker(config=Settings(neo4j_uri="bolt://127.0.0.1:7699"),
                                    db=self.db, name="worker-1", poll_seconds=0)

    def test_the_loop_leaves_when_asked(self):
        self.worker.stop()
        finished = threading.Event()

        def loop():
            self.worker.run_forever()
            finished.set()

        thread = threading.Thread(target=loop)
        thread.start()
        thread.join(timeout=5)
        self.assertTrue(finished.is_set(), "цикл не завершился")

    def test_the_job_in_hand_is_finished_first(self):
        job = store.enqueue(self.db, JobKind.PUBLISH, {"group": "2024"})

        def step(config, db, payload):
            self.worker.stop()
            return {"rows_persons": 3}

        with patch.dict(worker.STEPS, {JobKind.PUBLISH: step}):
            self.worker.run_forever()
        self.assertEqual(store.read(self.db, job.id).state, JobState.DONE)
