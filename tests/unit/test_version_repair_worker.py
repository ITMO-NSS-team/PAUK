import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import mongomock
from neo4j.exceptions import ServiceUnavailable
from pymongo.errors import AutoReconnect

from pauk.graph import version_repair as graph_repair
from pauk.jobs import locks, store, version_repairs, worker
from pauk.jobs.models import GRAPH, JobKind, JobState, VersionsPayload
from pauk.settings import Settings
from pauk.storage import version_repair


class VersionRepairWorkerTest(unittest.TestCase):
    """Only the graph boundary is mocked; intents, dispatch and jobs use Mongo semantics."""

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        self.moment = datetime(2026, 11, 19, 12, tzinfo=UTC)
        self.enterContext(patch.object(version_repair, "_now", lambda: self.moment))
        self.enterContext(patch.object(store, "now", lambda: self.moment))
        self.enterContext(patch.object(locks, "now", lambda: self.moment))
        self.client = self.enterContext(patch.object(graph_repair, "audited_client"))
        self.reconcile = self.enterContext(patch.object(
            graph_repair, "reconcile_version_pair", return_value=("version_held", 1, 0),
        ))
        self.worker = worker.Worker(Settings(neo4j_uri="bolt://127.0.0.1:7699"), self.db, name="repair-worker")

    def intent(self, *, collection="graph_overrides", document_id="decision", mode="current", due_in=0):
        intent = version_repair.make_intent(["W2", "W1"], mode=mode)
        intent["next_attempt_at"] = self.moment + timedelta(seconds=due_in)
        self.db[collection].update_one({"_id": document_id}, {"$set": {version_repair.FIELD: intent}}, upsert=True)
        return intent

    def pending(self, collection="graph_overrides", document_id="decision"):
        return self.db[collection].find_one({"_id": document_id}).get(version_repair.FIELD)

    def advance(self, seconds=60):
        self.moment += timedelta(seconds=seconds)

    def test_a_committed_intent_without_any_job_is_rediscovered_and_acknowledged(self):
        intent = self.intent()
        self.assertTrue(self.worker.run_once())
        self.assertIsNone(self.pending())
        job = store.recent(self.db)[0]
        self.assertEqual(job.kind, JobKind.VERSIONS)
        self.assertEqual(job.state, JobState.DONE)
        self.assertEqual(job.payload["repair"], {
            "collection_name": "graph_overrides", "document_id": "decision", "token": intent["token"],
        })
        self.assertEqual(job.payload["mode"], "current")
        self.assertEqual(job.result["version_repairs"], 1)
        self.assertFalse(self.reconcile.call_args.kwargs["reopen"])
        self.client.return_value.close.assert_called_once()

    def test_only_due_intents_are_dispatched(self):
        self.intent(due_in=60)
        self.assertFalse(self.worker.run_once())
        self.assertEqual(store.count(self.db), 0)
        self.advance()
        self.assertTrue(self.worker.run_once())
        self.assertIsNone(self.pending())

    def test_review_obligations_preserve_explicit_reopen_mode(self):
        self.intent(collection="review_pairs", document_id="question", mode="reopen")
        self.worker.run_once()
        self.assertIsNone(self.pending("review_pairs", "question"))
        self.assertTrue(self.reconcile.call_args.kwargs["reopen"])
        self.assertEqual(store.recent(self.db)[0].payload["mode"], "reopen")

    def test_the_worker_discovers_new_intents_on_every_poll(self):
        self.assertFalse(self.worker.run_once())
        self.intent()
        self.assertTrue(self.worker.run_once())
        self.assertEqual(self.reconcile.call_count, 1)

    def test_an_enqueue_failure_keeps_the_intent_and_retries_after_backoff(self):
        intent = self.intent()
        def fail_insert(*args, **kwargs):
            self.assertEqual(len(list(version_repair.pending(self.db, due_only=True))), 0)
            raise AutoReconnect("queue write failed")

        with patch.object(store, "enqueue", side_effect=fail_insert):
            self.assertFalse(self.worker.run_once())
        self.assertEqual(store.count(self.db), 0)
        pending = self.pending()
        self.assertEqual(pending["token"], intent["token"])
        self.assertIn("queue write failed", pending["last_error"])
        self.assertEqual(pending["next_attempt_at"], (self.moment + timedelta(seconds=60)).replace(tzinfo=None))
        self.assertFalse(self.worker.run_once())
        self.advance()
        self.assertTrue(self.worker.run_once())
        self.assertIsNone(self.pending())

    def test_losing_a_successful_enqueue_reply_does_not_lose_or_repeat_the_work(self):
        self.intent()
        enqueue = store.enqueue

        def inserted_then_disconnected(*args, **kwargs):
            enqueue(*args, **kwargs)
            raise AutoReconnect("insert reply lost")

        with patch.object(store, "enqueue", inserted_then_disconnected):
            self.assertTrue(self.worker.run_once())
        self.assertIsNone(self.pending())
        self.assertEqual(store.count(self.db), 1)
        self.assertFalse(self.worker.run_once())
        self.assertEqual(self.reconcile.call_count, 1)

    def test_a_failed_backoff_write_cannot_discard_the_intent(self):
        intent = self.intent()
        with patch.object(version_repair, "defer", side_effect=AutoReconnect("decision store offline")):
            self.assertFalse(self.worker.run_once())
        self.assertEqual(store.count(self.db), 0)
        self.assertEqual(self.pending()["token"], intent["token"])
        self.assertTrue(self.worker.run_once())
        self.assertIsNone(self.pending())

    def test_discovery_failure_does_not_stop_other_queued_work(self):
        job = store.enqueue(self.db, JobKind.PUBLISH, {"group": "g"})
        with (
            patch.object(version_repair, "pending", side_effect=AutoReconnect("scan failed")),
            patch.dict(worker.STEPS, {JobKind.PUBLISH: lambda *_: {"rows": 1}}),
        ):
            self.assertTrue(self.worker.run_once())
        self.assertEqual(store.read(self.db, job.id).state, JobState.DONE)

    def test_live_queued_claimed_and_running_attempts_are_not_duplicated(self):
        self.intent()
        self.assertEqual(version_repairs.dispatch_pending(self.db), 1)
        job = store.recent(self.db)[0]
        for state in (JobState.QUEUED, JobState.CLAIMED, JobState.RUNNING):
            with self.subTest(state=state), patch.object(store, "claim", return_value=None):
                self.advance()
                self.db[store.COLLECTION].update_one({"_id": job.id}, {"$set": {"state": str(state)}})
                self.assertFalse(self.worker.run_once())
                self.assertEqual(store.count(self.db), 1)
                self.assertEqual(len(list(version_repair.pending(self.db, due_only=True))), 0)
        self.reconcile.assert_not_called()

    def test_concurrent_dispatch_duplicates_are_safe_and_do_not_repeat_graph_work(self):
        self.intent()
        has_live = store.has_live_version_repair
        interleaved = []

        def dispatch_between_check_and_insert(db, repair):
            present = has_live(db, repair)
            if not present and not interleaved:
                interleaved.append(True)
                self.assertEqual(version_repairs.dispatch_pending(db), 1)
            return present

        with patch.object(store, "has_live_version_repair", dispatch_between_check_and_insert):
            self.assertTrue(self.worker.run_once())
        self.assertEqual(store.count(self.db), 2)
        self.assertTrue(self.worker.run_once())
        self.assertEqual(self.reconcile.call_count, 1)
        self.assertIsNone(self.pending())
        self.assertCountEqual([job.result for job in store.recent(self.db)], [
            {"version_edges": 1, "version_edges_removed": 0, "version_repairs": 1, "version_repairs_pending": 0},
            {"version_repairs_obsolete": 1},
        ])

    def test_a_cancelled_queued_attempt_does_not_cancel_the_saved_decision(self):
        intent = self.intent()
        version_repairs.dispatch_pending(self.db)
        first = store.recent(self.db)[0]
        store.request_cancel(self.db, first.id)
        self.assertFalse(self.worker.run_once())
        self.assertEqual(self.pending()["token"], intent["token"])
        self.advance()
        self.assertTrue(self.worker.run_once())
        self.assertIsNone(self.pending())
        self.assertEqual(store.read(self.db, first.id).state, JobState.CANCELLED)
        self.assertEqual(store.count(self.db), 2)

    def test_a_cancel_requested_before_execution_defers_without_running_the_repair(self):
        self.intent()
        version_repairs.dispatch_pending(self.db)
        job = store.recent(self.db)[0]
        self.db[store.COLLECTION].update_one({"_id": job.id}, {"$set": {"cancel_requested": True}})
        self.assertTrue(self.worker.run_once())
        self.assertEqual(store.read(self.db, job.id).state, JobState.CANCELLED)
        self.assertIsNotNone(self.pending())
        self.assertFalse(self.worker.run_once())
        self.reconcile.assert_not_called()
        self.advance()
        self.assertTrue(self.worker.run_once())
        self.assertIsNone(self.pending())

    def test_a_cancel_at_a_report_seam_defers_the_obligation(self):
        self.intent()

        def cancel_then_report(config, db, payload, stop, report):
            store.request_cancel(db, store.running(db)[0].id)
            report("before repair")
            self.fail("the cancellation seam was ignored")

        with patch.dict(worker.STEPS, {JobKind.VERSIONS: cancel_then_report}):
            self.assertTrue(self.worker.run_once())
        job = store.recent(self.db)[0]
        self.assertEqual(job.state, JobState.CANCELLED)
        self.assertIsNotNone(self.pending())
        self.assertFalse(self.worker.run_once())
        self.advance()
        self.assertTrue(self.worker.run_once())
        self.assertIsNone(self.pending())

    def stale_attempt(self, *, cancelled):
        self.intent()
        version_repairs.dispatch_pending(self.db)
        first = store.claim(self.db, "gone-worker")
        store.start(self.db, first.id)
        if cancelled:
            store.request_cancel(self.db, first.id)
        self.advance((locks.LEASE_MINUTES + 1) * 60)
        self.assertTrue(self.worker.run_once())
        self.assertEqual(store.read(self.db, first.id).state,
                         JobState.CANCELLED if cancelled else JobState.FAILED)
        self.assertEqual(store.count(self.db), 2)
        self.assertIsNone(self.pending())
        self.assertEqual(self.reconcile.call_count, 1)

    def test_a_reaped_running_attempt_is_rediscovered_before_claiming(self):
        self.stale_attempt(cancelled=False)

    def test_a_cancelled_stale_attempt_does_not_erase_the_obligation(self):
        self.stale_attempt(cancelled=True)

    def test_a_transient_graph_failure_retries_the_same_attempt_after_delay(self):
        self.intent()
        self.reconcile.side_effect = [ServiceUnavailable("graph offline"), ("version_held", 1, 0)]
        self.assertTrue(self.worker.run_once())
        first = store.recent(self.db)[0]
        self.assertEqual(first.state, JobState.QUEUED)
        self.assertIn("graph offline", self.pending()["last_error"])
        self.assertFalse(self.worker.run_once())
        self.advance()
        self.assertTrue(self.worker.run_once())
        self.assertEqual(store.count(self.db), 1)
        self.assertEqual(store.read(self.db, first.id).state, JobState.DONE)
        self.assertIsNone(self.pending())

    def test_a_permanent_error_is_visible_and_does_not_spin_or_acknowledge(self):
        self.intent()
        self.reconcile.side_effect = ValueError("publication authority is invalid")
        self.assertTrue(self.worker.run_once())
        first = store.recent(self.db)[0]
        self.assertEqual(first.state, JobState.FAILED)
        self.assertIn("publication authority is invalid", first.error)
        self.assertIn("publication authority is invalid", self.pending()["last_error"])
        self.assertFalse(self.worker.run_once())
        self.assertEqual(store.count(self.db), 1)
        self.advance()
        self.reconcile.side_effect = None
        self.assertTrue(self.worker.run_once())
        self.assertEqual(store.count(self.db), 2)
        self.assertEqual(store.read(self.db, first.id).state, JobState.FAILED)
        self.assertIsNone(self.pending())

    def test_an_invalid_intent_mode_is_visible_and_kept_for_retry(self):
        self.intent()
        self.db.graph_overrides.update_one({"_id": "decision"}, {
            "$set": {f"{version_repair.FIELD}.mode": "invalid"},
        })
        self.assertFalse(self.worker.run_once())
        self.assertEqual(store.count(self.db), 0)
        self.assertIn("ValidationError", self.pending()["last_error"])
        self.assertFalse(self.worker.run_once())
        self.reconcile.assert_not_called()

    def test_a_new_token_is_not_hidden_by_a_live_old_token(self):
        first_intent = self.intent()
        version_repairs.dispatch_pending(self.db)
        first = store.recent(self.db)[0]
        self.advance(1)
        second_intent = self.intent()
        self.assertTrue(self.worker.run_once())
        self.assertEqual(store.read(self.db, first.id).result, {"version_repairs_obsolete": 1})
        self.assertEqual(self.pending()["token"], second_intent["token"])
        self.assertFalse(version_repair.ack(self.db, "graph_overrides", "decision", first_intent["token"]))
        self.assertEqual(store.count(self.db), 2)
        self.assertTrue(self.worker.run_once())
        self.assertIsNone(self.pending())
        self.assertEqual(self.reconcile.call_count, 1)

    def test_supersession_during_graph_sync_cannot_ack_the_new_intent(self):
        first_intent = self.intent(mode="reopen")
        newer = []

        def supersede(client, db, members, **kwargs):
            self.assertEqual(self.pending()["token"], first_intent["token"])
            newer.append(self.intent(mode="current"))
            return "version_held", 1, 0

        self.reconcile.side_effect = supersede
        self.assertTrue(self.worker.run_once())
        first = store.recent(self.db)[0]
        self.assertEqual(first.result["version_repairs_pending"], 1)
        self.assertEqual(self.pending()["token"], newer[0]["token"])
        self.assertTrue(self.reconcile.call_args.kwargs["reopen"])
        self.reconcile.side_effect = None
        self.assertTrue(self.worker.run_once())
        self.assertFalse(self.reconcile.call_args.kwargs["reopen"])
        self.assertIsNone(self.pending())

    def test_an_old_handler_failure_cannot_backoff_the_new_generation(self):
        first_intent = self.intent()
        newer = []

        def fail_after_supersession(config, db, payload, stop, report):
            newer.append(self.intent())
            raise ValueError("old handler failed")

        with patch.dict(worker.STEPS, {JobKind.VERSIONS: fail_after_supersession}):
            self.worker.run_once()
        self.assertNotEqual(first_intent["token"], newer[0]["token"])
        pending = self.pending()
        self.assertEqual(pending["token"], newer[0]["token"])
        self.assertNotIn("last_error", pending)
        self.assertEqual(len(list(version_repair.pending(self.db, due_only=True))), 1)
        self.assertTrue(self.worker.run_once())
        self.assertIsNone(self.pending())

    def test_a_repair_uses_neither_the_full_pass_nor_legacy_pair_or_llm_entrypoints(self):
        self.intent()
        with (
            patch("pauk.graph.version_link.run_version_link") as full,
            patch("pauk.graph.version_link.run_version_pair") as pair,
            patch("pauk.sources.llm.check_providers") as providers,
        ):
            self.worker.run_once()
        full.assert_not_called()
        pair.assert_not_called()
        providers.assert_not_called()
        self.assertEqual(self.reconcile.call_count, 1)

    def test_a_held_graph_keeps_one_queued_attempt_until_it_is_free(self):
        self.intent()
        with locks.held(self.db, GRAPH, "other-writer"):
            self.assertFalse(self.worker.run_once())
            self.advance()
            self.assertFalse(self.worker.run_once())
            self.assertEqual(store.count(self.db), 1)
            self.reconcile.assert_not_called()
        self.assertTrue(self.worker.run_once())
        self.assertIsNone(self.pending())


class VersionsDispatchModeTest(unittest.TestCase):
    def setUp(self):
        self.config = Settings()
        self.db = mongomock.MongoClient()["pauk_test"]

    def perform(self, payload):
        return worker._versions(self.config, self.db, VersionsPayload.model_validate(payload),
                                lambda: False, lambda *_: None)

    def test_legacy_and_explicit_reopen_pairs_preserve_the_old_call_signature(self):
        for extra in ({}, {"mode": "reopen"}):
            with self.subTest(extra=extra), patch("pauk.graph.version_link.run_version_pair", return_value={}) as pair:
                self.perform({"members": ["W1", "W2"], **extra})
                pair.assert_called_once_with(self.config, self.db, ("W1", "W2"))

    def test_current_pair_mode_disables_reopen_explicitly(self):
        with patch("pauk.graph.version_link.run_version_pair", return_value={}) as pair:
            self.perform({"members": ["W1", "W2"], "mode": "current"})
        pair.assert_called_once_with(self.config, self.db, ("W1", "W2"), reopen=False)

    def test_full_versions_jobs_keep_their_llm_flag_and_call_signature(self):
        with patch("pauk.graph.version_link.run_version_link", return_value={"version_edges": 2}) as full:
            self.assertEqual(self.perform({"llm": True, "mode": "current"}), {"version_edges": 2})
        full.assert_called_once_with(self.config, self.db, llm=True)

    def test_a_repair_reference_takes_precedence_over_members_and_ignores_stale_job_mode(self):
        repair = {"collection_name": "review_pairs", "document_id": "question", "token": "token"}
        with (
            patch.object(graph_repair, "run_pending_version_repair", return_value={}) as pending,
            patch("pauk.graph.version_link.run_version_pair") as pair,
        ):
            self.perform({"members": ["old", "members"], "repair": repair, "mode": "reopen", "llm": True})
        pending.assert_called_once_with(self.config, self.db, "review_pairs", "question", "token")
        pair.assert_not_called()
