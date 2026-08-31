import unittest
from datetime import timedelta
from unittest.mock import patch

import mongomock
from pydantic import ValidationError

from pauk.jobs import locks, store
from pauk.jobs.models import (
    GRAPH,
    Job,
    JobKind,
    JobState,
    aware,
    now,
    parse_payload,
    resource_for,
)
from pauk.settings import Settings


class LockTest(unittest.TestCase):
    """Only one runner writes the graph at a time."""

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]

    def test_a_free_resource_is_taken(self):
        self.assertTrue(locks.acquire(self.db, GRAPH, "worker-1"))

    def test_a_held_resource_is_refused(self):
        locks.acquire(self.db, GRAPH, "worker-1")
        self.assertFalse(locks.acquire(self.db, GRAPH, "worker-2"))

    def test_the_first_holder_keeps_it(self):
        locks.acquire(self.db, GRAPH, "worker-1")
        locks.acquire(self.db, GRAPH, "worker-2")
        self.assertEqual(locks.holder(self.db, GRAPH)["owner"], "worker-1")

    def test_releasing_frees_it(self):
        locks.acquire(self.db, GRAPH, "worker-1")
        self.assertTrue(locks.release(self.db, GRAPH, "worker-1"))
        self.assertTrue(locks.acquire(self.db, GRAPH, "worker-2"))

    def test_only_the_holder_can_release(self):
        locks.acquire(self.db, GRAPH, "worker-1")
        self.assertFalse(locks.release(self.db, GRAPH, "worker-2"))
        self.assertEqual(locks.holder(self.db, GRAPH)["owner"], "worker-1")

    def test_two_resources_do_not_block_each_other(self):
        locks.acquire(self.db, GRAPH, "worker-1")
        self.assertTrue(locks.acquire(self.db, "group:2024", "worker-2"))

    def expire(self, resource):
        """Age the lease out, as a machine dying mid-run would."""
        self.db[locks.COLLECTION].update_one(
            {"_id": resource},
            {"$set": {"expires_at": now() - timedelta(minutes=1)}})

    def test_an_expired_lock_is_taken_over(self):
        locks.acquire(self.db, GRAPH, "worker-1")
        self.expire(GRAPH)
        self.assertTrue(locks.acquire(self.db, GRAPH, "worker-2"))
        self.assertEqual(locks.holder(self.db, GRAPH)["owner"], "worker-2")

    def test_an_expired_lock_reads_as_free(self):
        locks.acquire(self.db, GRAPH, "worker-1")
        self.expire(GRAPH)
        self.assertIsNone(locks.holder(self.db, GRAPH))

    def test_renewing_keeps_it(self):
        locks.acquire(self.db, GRAPH, "worker-1")
        self.expire(GRAPH)
        self.assertTrue(locks.renew(self.db, GRAPH, "worker-1"))
        self.assertFalse(locks.acquire(self.db, GRAPH, "worker-2"))

    def test_only_the_holder_can_renew(self):
        locks.acquire(self.db, GRAPH, "worker-1")
        self.assertFalse(locks.renew(self.db, GRAPH, "worker-2"))

    def test_a_stored_time_without_a_zone_does_not_raise(self):
        # pymongo hands datetimes back naive; comparing one with an aware
        # now() is a TypeError, not an answer.
        locks.acquire(self.db, GRAPH, "worker-1")
        row = self.db[locks.COLLECTION].find_one({"_id": GRAPH})
        self.db[locks.COLLECTION].update_one(
            {"_id": GRAPH},
            {"$set": {"expires_at": aware(row["expires_at"]).replace(tzinfo=None)}})
        self.assertIsNotNone(locks.holder(self.db, GRAPH))


class HeldTest(unittest.TestCase):
    """The block form: taken on the way in, given back on the way out."""

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]

    def test_it_is_free_again_afterwards(self):
        with locks.held(self.db, GRAPH, "worker-1"):
            self.assertIsNotNone(locks.holder(self.db, GRAPH))
        self.assertIsNone(locks.holder(self.db, GRAPH))

    def test_it_is_free_again_after_a_failure(self):
        with self.assertRaises(RuntimeError), locks.held(self.db, GRAPH, "worker-1"):
            raise RuntimeError("the publish blew up")
        self.assertIsNone(locks.holder(self.db, GRAPH))

    def test_a_second_block_is_refused(self):
        with locks.held(self.db, GRAPH, "worker-1"), self.assertRaises(locks.Busy), \
                locks.held(self.db, GRAPH, "worker-2"):
            pass

    def test_the_refusal_names_the_holder(self):
        with locks.held(self.db, GRAPH, "worker-1"), \
                self.assertRaises(locks.Busy) as caught, \
                locks.held(self.db, GRAPH, "worker-2"):
            pass
        self.assertIn("worker-1", str(caught.exception))


class PayloadTest(unittest.TestCase):
    """A payload arrives from a form, so it is checked, not trusted."""

    def test_a_publish_needs_a_group(self):
        with self.assertRaises(ValidationError):
            parse_payload(JobKind.PUBLISH, {})

    def test_a_group_name_is_validated(self):
        with self.assertRaises(ValidationError):
            parse_payload(JobKind.PUBLISH, {"group": "../etc"})

    def test_a_dedup_takes_nothing(self):
        self.assertIsNotNone(parse_payload(JobKind.DEDUP, {}))

    def test_a_map_run_defaults_to_private(self):
        self.assertFalse(parse_payload(JobKind.MAP, {}).public)

    def test_a_collection_run_holds_only_its_group(self):
        payload = parse_payload(JobKind.COLLECT, {"group": "2024", "work_id": "W1"})
        self.assertEqual(resource_for(JobKind.COLLECT, payload), "group:2024")

    def test_everything_else_holds_the_graph(self):
        for kind, payload in ((JobKind.PUBLISH, {"group": "2024"}),
                              (JobKind.DEDUP, {}), (JobKind.MAP, {})):
            with self.subTest(kind=kind):
                self.assertEqual(resource_for(kind, parse_payload(kind, payload)), GRAPH)

    def test_a_path_cannot_be_asked_for(self):
        # --works-file names a file on the machine running the pipeline, and
        # a path arriving from a browser is a way to read unrelated files.
        payload = parse_payload(JobKind.COLLECT, {"group": "2024", "works_file": "/etc/passwd"})
        self.assertFalse(hasattr(payload, "works_file"))


class QueueTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]

    def queue(self, group="2024", actor="user:roman"):
        return store.enqueue(self.db, JobKind.PUBLISH, {"group": group}, actor=actor)

    def test_a_queued_job_is_readable(self):
        job = self.queue()
        self.assertEqual(store.read(self.db, job.id).state, JobState.QUEUED)

    def test_the_resource_is_derived_not_given(self):
        self.assertEqual(self.queue().resource, GRAPH)

    def test_a_bad_payload_is_refused_before_it_is_stored(self):
        with self.assertRaises(ValidationError):
            store.enqueue(self.db, JobKind.PUBLISH, {})
        self.assertEqual(store.count(self.db), 0)

    def age(self, job_id, minutes):
        """Move a job back in time, so order is order and not coincidence."""
        self.db[store.COLLECTION].update_one(
            {"_id": job_id}, {"$set": {"created_at": now() - timedelta(minutes=minutes)}})

    def test_claiming_takes_the_oldest_first(self):
        first, second = self.queue("2023"), self.queue("2024")
        self.age(first.id, 10)
        self.age(second.id, 5)
        self.assertEqual(store.claim(self.db, "worker-1").id, first.id)
        self.assertEqual(store.claim(self.db, "worker-2").id, second.id)

    def test_claiming_is_a_single_command(self):
        """Two workers racing cannot be shown here: mongomock never leaves
        the window open, with the atomic version or a broken one. What can
        be shown is that there is no window — selecting the job and marking
        it claimed are one command, which is what makes it atomic.
        """
        calls = []

        class Watched:
            def __init__(self, collection):
                self._collection = collection

            def __getattr__(self, name):
                attribute = getattr(self._collection, name)
                if not callable(attribute):
                    return attribute

                def record(*args, **kwargs):
                    calls.append(name)
                    return attribute(*args, **kwargs)
                return record

        class WatchedDb:
            def __init__(self, db):
                self._db = db

            def __getitem__(self, name):
                return Watched(self._db[name])

        self.queue()
        calls.clear()
        store.claim(WatchedDb(self.db), "worker-1")
        self.assertEqual(calls, ["find_one_and_update"])

    def test_a_job_is_claimed_once(self):
        self.queue()
        self.assertIsNotNone(store.claim(self.db, "worker-1"))
        self.assertIsNone(store.claim(self.db, "worker-2"))

    def test_an_empty_queue_claims_nothing(self):
        self.assertIsNone(store.claim(self.db, "worker-1"))

    def test_starting_marks_it_running(self):
        job = self.queue()
        store.claim(self.db, "worker-1")
        self.assertTrue(store.start(self.db, job.id))
        self.assertEqual(store.read(self.db, job.id).state, JobState.RUNNING)

    def test_a_job_that_was_not_claimed_cannot_start(self):
        job = self.queue()
        self.assertFalse(store.start(self.db, job.id))

    def test_requeueing_lets_another_worker_take_it(self):
        job = self.queue()
        store.claim(self.db, "worker-1")
        self.assertTrue(store.requeue(self.db, job.id))
        self.assertEqual(store.claim(self.db, "worker-2").id, job.id)

    def test_a_heartbeat_only_counts_while_running(self):
        job = self.queue()
        store.claim(self.db, "worker-1")
        self.assertFalse(store.heartbeat(self.db, job.id))
        store.start(self.db, job.id)
        self.assertTrue(store.heartbeat(self.db, job.id))

    def test_finishing_keeps_the_counts(self):
        job = self.queue()
        store.claim(self.db, "worker-1")
        store.start(self.db, job.id)
        store.finish(self.db, job.id, {"nodes": 12})
        stored = store.read(self.db, job.id)
        self.assertEqual(stored.state, JobState.DONE)
        self.assertEqual(stored.result, {"nodes": 12})

    def test_failing_keeps_the_message(self):
        job = self.queue()
        store.fail(self.db, job.id, "Neo4j unreachable")
        stored = store.read(self.db, job.id)
        self.assertEqual(stored.state, JobState.FAILED)
        self.assertEqual(stored.error, "Neo4j unreachable")

    def test_a_finished_job_does_not_change_again(self):
        job = self.queue()
        store.finish(self.db, job.id, {"nodes": 1})
        self.assertFalse(store.fail(self.db, job.id, "too late"))
        self.assertEqual(store.read(self.db, job.id).state, JobState.DONE)

    def test_cancelling_a_queued_job_ends_it_outright(self):
        job = self.queue()
        self.assertTrue(store.request_cancel(self.db, job.id))
        self.assertEqual(store.read(self.db, job.id).state, JobState.CANCELLED)

    def test_cancelling_a_running_job_only_asks(self):
        job = self.queue()
        store.claim(self.db, "worker-1")
        store.start(self.db, job.id)
        self.assertTrue(store.request_cancel(self.db, job.id))
        stored = store.read(self.db, job.id)
        self.assertEqual(stored.state, JobState.RUNNING)
        self.assertTrue(stored.cancel_requested)

    def test_running_lists_what_is_under_way(self):
        job = self.queue()
        later = self.queue("2025")
        self.age(job.id, 10)
        self.age(later.id, 5)
        self.assertEqual(store.claim(self.db, "worker-1").id, job.id)
        store.start(self.db, job.id)
        self.assertEqual([row.id for row in store.running(self.db)], [job.id])

    def test_running_can_be_asked_about_one_resource(self):
        job = self.queue()
        store.claim(self.db, "worker-1")
        store.start(self.db, job.id)
        self.assertEqual(len(store.running(self.db, resource=GRAPH)), 1)
        self.assertEqual(store.running(self.db, resource="group:2024"), [])

    def test_the_history_is_newest_first(self):
        first, second = self.queue("2023"), self.queue("2024")
        self.age(first.id, 10)
        self.age(second.id, 5)
        self.assertEqual([row.id for row in store.recent(self.db)], [second.id, first.id])

    def test_jobs_queued_in_the_same_millisecond_keep_a_stable_order(self):
        # Not a meaningful order — there is none — but the page must not
        # shuffle rows between two refreshes.
        for _ in range(5):
            self.queue()
        first = [row.id for row in store.recent(self.db)]
        self.assertEqual(first, [row.id for row in store.recent(self.db)])

    def test_the_history_can_be_filtered(self):
        self.queue(actor="user:roman")
        self.queue(actor="user:petrov")
        self.assertEqual(store.count(self.db, actor="user:roman"), 1)
        self.assertEqual(len(store.recent(self.db, actor="user:roman")), 1)

    def test_a_stored_job_round_trips(self):
        # Written through model_dump and read through model_validate: a
        # value that survives one and not the other breaks a page, not a
        # test, and does it on the day somebody uses the field.
        job = self.queue()
        self.assertIsInstance(store.read(self.db, job.id), Job)
        self.assertEqual(store.read(self.db, job.id).payload, {"group": "2024"})


class GraphIsHeldWhilePublishingTest(unittest.TestCase):
    """The lock lives with the pipeline functions, not with the worker.

    The likeliest collision is not two workers — there is one — but somebody
    running `pauk publish graph` in a terminal while the panel schedules the
    same thing. Neither goes through the other's code, and both go through
    these.
    """

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        self.config = Settings()

    def test_a_publish_is_refused_while_the_graph_is_held(self):
        from pauk.graph.load import load_jsonl_group
        with locks.held(self.db, GRAPH, "someone-else"), self.assertRaises(locks.Busy):
            load_jsonl_group(self.config, self.db, "2024")

    def test_a_dedup_is_refused_while_the_graph_is_held(self):
        from pauk.graph.dedup import run_graph_dedup
        with locks.held(self.db, GRAPH, "someone-else"), self.assertRaises(locks.Busy):
            run_graph_dedup(self.config, self.db)

    def test_a_publish_runs_when_the_graph_is_free(self):
        from pauk.graph import load
        with patch.object(load, "_load_locked") as work:
            load.load_jsonl_group(self.config, self.db, "2024")
        work.assert_called_once()

    def test_the_graph_is_free_again_afterwards(self):
        from pauk.graph import load
        with patch.object(load, "_load_locked"):
            load.load_jsonl_group(self.config, self.db, "2024")
        self.assertIsNone(locks.holder(self.db, GRAPH))

    def test_the_graph_is_free_again_after_a_failed_publish(self):
        from pauk.graph import load
        with patch.object(load, "_load_locked", side_effect=RuntimeError("boom")), \
                self.assertRaises(RuntimeError):
            load.load_jsonl_group(self.config, self.db, "2024")
        self.assertIsNone(locks.holder(self.db, GRAPH))

    def test_a_publish_and_a_collection_run_do_not_block_each_other(self):
        from pauk.graph import load
        with locks.held(self.db, "group:2024", "collector"), \
                patch.object(load, "_load_locked") as work:
            load.load_jsonl_group(self.config, self.db, "2024")
        work.assert_called_once()
