import threading
import unittest
from datetime import timedelta
from unittest.mock import patch

import mongomock
from pymongo.errors import AutoReconnect

from pauk.jobs import locks, store, worker
from pauk.jobs.models import GRAPH, PAYLOADS, JobKind, JobState, PublishPayload, now
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
        def run(config, db, payload, stop):
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

        def cancel_then_wait(config, db, payload, stop):
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

        def settle_then_claim(db, name, busy=None):
            claimed = original(db, name, busy)
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

        def step(config, db, payload, stop):
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
        # The lock is taken inside the step, the way the real functions
        # take it: taking it first would leave the resource busy and the
        # worker would pass the job over instead of claiming it.
        store.enqueue(self.db, JobKind.PUBLISH, {"group": "2024"})
        beaten = threading.Event()
        first = []

        def step(config, db, payload, stop):
            locks.acquire(db, GRAPH, "worker-1")
            first.append(self.db[locks.COLLECTION].find_one({"_id": GRAPH})["expires_at"])
            beaten.wait(timeout=2)
            return {}

        seen = []
        with patch.object(worker, "BEAT_SECONDS", 0.01), \
                patch.dict(worker.STEPS, {JobKind.PUBLISH: step}):
            thread = threading.Thread(target=self.worker.run_once)
            thread.start()
            try:
                for _ in range(300):
                    row = self.db[locks.COLLECTION].find_one({"_id": GRAPH})
                    if row and first and row["expires_at"] != first[0]:
                        seen.append(row["expires_at"])
                        break
                    threading.Event().wait(0.01)
            finally:
                beaten.set()
                thread.join(timeout=5)
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

        def step(config, db, payload, stop):
            self.worker.stop()
            return {"rows_persons": 3}

        with patch.dict(worker.STEPS, {JobKind.PUBLISH: step}):
            self.worker.run_forever()
        self.assertEqual(store.read(self.db, job.id).state, JobState.DONE)


class PipelineJobTest(unittest.TestCase):
    """Collect, publish, rebuild the map — one job, three phases.

    Not three queued jobs: publishing names a group, and when the queue is
    filled that group has no rows for the check to accept.
    """

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        self.worker = worker.Worker(config=Settings(neo4j_uri="bolt://127.0.0.1:7699"),
                                    db=self.db, name="worker-1", poll_seconds=0)
        self.done = []

    def queue(self):
        return store.enqueue(self.db, JobKind.PIPELINE,
                             {"group": "2026-08-30__W1", "work_id": "W1"},
                             actor="user:chief")

    def phase(self, name, result=None, cancels=None, raises=None):
        def step(config, db, payload, stop):
            self.done.append(name)
            if cancels is not None:
                store.request_cancel(db, cancels)
            if raises is not None:
                raise raises
            return result or {}
        return step

    def run_phases(self, **overrides):
        steps = {"_collect": self.phase("collect", {"raw_works": 5}),
                 "_publish": self.phase("publish", {"rows_persons": 5}),
                 "_rebuild_map": self.phase("map", {"map_authors": 5})}
        steps.update(overrides)
        with patch.multiple(worker, **steps):
            self.worker.run_once()

    def test_the_phases_run_in_order(self):
        self.queue()
        self.run_phases()
        self.assertEqual(self.done, ["collect", "publish", "map"])

    def test_the_counts_of_all_three_come_back(self):
        job = self.queue()
        self.run_phases()
        self.assertEqual(store.read(self.db, job.id).result,
                         {"raw_works": 5, "rows_persons": 5, "map_authors": 5})

    def test_it_is_one_job_not_three(self):
        self.queue()
        self.run_phases()
        self.assertEqual(store.count(self.db), 1)

    def test_cancelling_during_the_collection_stops_before_publishing(self):
        job = self.queue()
        self.run_phases(_collect=self.phase("collect", cancels=job.id))
        self.assertEqual(self.done, ["collect"])
        self.assertEqual(store.read(self.db, job.id).state, JobState.CANCELLED)

    def test_cancelling_during_the_publish_stops_before_the_map(self):
        job = self.queue()
        self.run_phases(_publish=self.phase("publish", cancels=job.id))
        self.assertEqual(self.done, ["collect", "publish"])
        self.assertEqual(store.read(self.db, job.id).state, JobState.CANCELLED)

    def test_a_phase_already_under_way_is_never_abandoned(self):
        # The check sits between phases, so a half-written publish cannot
        # be left behind by pressing cancel.
        job = self.queue()
        self.run_phases(_publish=self.phase("publish", cancels=job.id))
        self.assertIn("publish", self.done)

    def test_a_failing_phase_stops_the_rest(self):
        job = self.queue()
        self.run_phases(_publish=self.phase("publish", raises=RuntimeError("boom")))
        self.assertEqual(self.done, ["collect", "publish"])
        self.assertEqual(store.read(self.db, job.id).state, JobState.FAILED)

    def test_it_contends_for_the_graph(self):
        # It ends by publishing, so the panel has to warn while it runs.
        self.assertEqual(self.queue().resource, GRAPH)


class AbandonedJobTest(unittest.TestCase):
    """A job whose worker is gone must not sit in "under way" for ever.

    Nothing else ever moved a job out of that state: the only process that
    could was the one that died holding it. Stopping the worker while a run
    was going left the row there, and pressing cancel only added a line
    saying somebody had asked it to stop.
    """

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        self.worker = worker.Worker(config=Settings(neo4j_uri="bolt://127.0.0.1:7699"),
                                    db=self.db, name="worker-2", poll_seconds=0)

    def abandoned(self, minutes=20, cancelled=False):
        job = store.enqueue(self.db, JobKind.PUBLISH, {"group": "g"}, actor="user:chief")
        store.claim(self.db, "worker-1")
        store.start(self.db, job.id)
        if cancelled:
            store.request_cancel(self.db, job.id)
        self.db[store.COLLECTION].update_one(
            {"_id": job.id},
            {"$set": {"heartbeat_at": now() - timedelta(minutes=minutes)}})
        return job

    def test_a_job_that_still_beats_is_left_alone(self):
        job = self.abandoned(minutes=0)
        self.assertEqual(store.reap_stale(self.db), 0)
        self.assertEqual(store.read(self.db, job.id).state, JobState.RUNNING)

    def test_a_silent_job_is_recorded_as_failed(self):
        job = self.abandoned()
        self.assertEqual(store.reap_stale(self.db), 1)
        stored = store.read(self.db, job.id)
        self.assertEqual(stored.state, JobState.FAILED)
        self.assertIn("отвеча", stored.error)

    def test_a_silent_job_somebody_cancelled_is_recorded_as_cancelled(self):
        job = self.abandoned(cancelled=True)
        store.reap_stale(self.db)
        self.assertEqual(store.read(self.db, job.id).state, JobState.CANCELLED)

    def test_it_leaves_the_list_of_what_is_under_way(self):
        self.abandoned()
        store.reap_stale(self.db)
        self.assertEqual(store.running(self.db), [])

    def test_the_worker_clears_them_on_its_way_past(self):
        # Starting the worker again is the ordinary way this gets noticed.
        job = self.abandoned()
        self.worker.run_once()
        self.assertEqual(store.read(self.db, job.id).state, JobState.FAILED)

    def test_a_finished_job_is_never_touched(self):
        job = store.enqueue(self.db, JobKind.MAP, {})
        store.claim(self.db, "worker-1")
        store.start(self.db, job.id)
        store.finish(self.db, job.id, {"map_authors": 1})
        self.db[store.COLLECTION].update_one(
            {"_id": job.id}, {"$set": {"heartbeat_at": now() - timedelta(minutes=99)}})
        self.assertEqual(store.reap_stale(self.db), 0)
        self.assertEqual(store.read(self.db, job.id).state, JobState.DONE)

    def test_a_job_claimed_but_never_started_is_reaped_too(self):
        # The worker can die between taking the document and taking the
        # resource; there is no heartbeat after the claim to go by.
        job = store.enqueue(self.db, JobKind.DEDUP, {})
        store.claim(self.db, "worker-1")
        self.db[store.COLLECTION].update_one(
            {"_id": job.id}, {"$set": {"heartbeat_at": now() - timedelta(minutes=20)}})
        store.reap_stale(self.db)
        self.assertEqual(store.read(self.db, job.id).state, JobState.FAILED)


class SeveralRunsAtOnceTest(unittest.TestCase):
    """What several people launching together run into.

    Three separate faults, all of them about locks and time: a beat that
    dies of one blip, a blocked job holding up the ones behind it, and a
    verdict of "abandoned" passed while the lease is still alive.
    """

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        self.worker = worker.Worker(config=Settings(neo4j_uri="bolt://127.0.0.1:7699"),
                                    db=self.db, name="worker-1", poll_seconds=0)
        self.done = []

    def records(self, name):
        def step(config, db, payload, stop):
            self.done.append(name)
            return {}
        return step

    def test_one_blip_does_not_end_the_beating(self):
        # An unhandled error killed the thread outright, and the run then
        # went on in silence until the lease it held expired under it.
        job = store.enqueue(self.db, JobKind.PUBLISH, {"group": "g"})
        store.claim(self.db, "worker-1")
        store.start(self.db, job.id)
        tries = []

        def flaky(db, job_id):
            tries.append(len(tries))
            if len(tries) <= 3:
                raise AutoReconnect("blip")
            return True

        with patch.object(worker, "BEAT_SECONDS", 0.01), \
                patch.object(worker.store, "heartbeat", flaky), \
                worker._Beat(self.db, store.read(self.db, job.id), "worker-1"):
            threading.Event().wait(0.2)
        self.assertGreater(len(tries), 5, "поток отметок умер на первой ошибке")

    def test_a_blocked_job_does_not_hold_up_the_rest(self):
        store.enqueue(self.db, JobKind.PUBLISH, {"group": "g"})
        store.enqueue(self.db, JobKind.COLLECT, {"group": "2024", "work_id": "W1"})
        locks.acquire(self.db, GRAPH, "somebody-in-a-terminal")
        with patch.dict(worker.STEPS, {JobKind.PUBLISH: self.records("publish"),
                                       JobKind.COLLECT: self.records("collect")}):
            for _ in range(3):
                self.worker.run_once()
        self.assertEqual(self.done, ["collect"])

    def test_the_blocked_job_goes_as_soon_as_the_resource_frees_up(self):
        job = store.enqueue(self.db, JobKind.PUBLISH, {"group": "g"})
        locks.acquire(self.db, GRAPH, "somebody-in-a-terminal")
        with patch.dict(worker.STEPS, {JobKind.PUBLISH: self.records("publish")}):
            self.worker.run_once()
            self.assertEqual(store.read(self.db, job.id).state, JobState.QUEUED)
            locks.release(self.db, GRAPH, "somebody-in-a-terminal")
            self.worker.run_once()
        self.assertEqual(self.done, ["publish"])

    def test_an_expired_lock_does_not_hold_anything_up(self):
        store.enqueue(self.db, JobKind.PUBLISH, {"group": "g"})
        locks.acquire(self.db, GRAPH, "a-worker-that-died")
        self.db[locks.COLLECTION].update_one(
            {"_id": GRAPH}, {"$set": {"expires_at": now() - timedelta(minutes=1)}})
        with patch.dict(worker.STEPS, {JobKind.PUBLISH: self.records("publish")}):
            self.worker.run_once()
        self.assertEqual(self.done, ["publish"])

    def test_a_quiet_job_is_not_buried_while_its_lease_could_be_alive(self):
        # Five minutes of silence is enough to warn about and not enough to
        # act on: the run may still hold the graph and still be writing.
        job = store.enqueue(self.db, JobKind.PUBLISH, {"group": "g"})
        store.claim(self.db, "worker-1")
        store.start(self.db, job.id)
        self.db[store.COLLECTION].update_one(
            {"_id": job.id}, {"$set": {"heartbeat_at": now() - timedelta(minutes=6)}})
        stored = store.read(self.db, job.id)
        self.assertTrue(store.is_quiet(stored), "страница должна предупредить")
        self.assertEqual(store.reap_stale(self.db), 0, "но хоронить рано")

    def test_it_is_buried_once_the_lease_cannot_be_alive(self):
        job = store.enqueue(self.db, JobKind.PUBLISH, {"group": "g"})
        store.claim(self.db, "worker-1")
        store.start(self.db, job.id)
        self.db[store.COLLECTION].update_one(
            {"_id": job.id},
            {"$set": {"heartbeat_at": now() - timedelta(minutes=locks.LEASE_MINUTES + 1)}})
        self.assertEqual(store.reap_stale(self.db), 1)
        self.assertEqual(store.read(self.db, job.id).state, JobState.FAILED)
