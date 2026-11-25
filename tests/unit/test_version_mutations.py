import argparse
import tempfile
import unittest
from contextlib import ExitStack, contextmanager, redirect_stdout
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import mongomock
from fastapi.testclient import TestClient
from pymongo.errors import AutoReconnect

from pauk.admin import cli as admin_cli
from pauk.admin import deps, nodes
from pauk.admin.app import build
from pauk.admin.auth import COOKIE, SESSIONS, create_user, session_key
from pauk.graph import overrides, version_link
from pauk.graph import version_repair as graph_repair
from pauk.jobs import locks
from pauk.jobs.models import GRAPH
from pauk.settings import Settings
from pauk.storage import review, version_repair
from tests.unit.admin_forms import RenderedForms
from tests.unit.test_admin_nodes import FakePanelGraph

VERSION = ("Publication", "IS_VERSION", "Publication")
EDGE = (*VERSION, "W2", "W1")


def parse_cli(*argv):
    parser = argparse.ArgumentParser()
    admin_cli.add_parser(parser.add_subparsers(dest="command", required=True))
    return parser.parse_args(["admin", *argv])


class _MutationGraph(FakePanelGraph):
    """The panel fake, with optional IDs for the full reconciliation entry point."""

    def fetch_publications_for_dedup(self, ids=None):
        if ids is None:
            ids = [node_id for label, node_id in self.nodes if label == "Publication"]
        return super().fetch_publications_for_dedup(ids)

    def fetch_version_edges(self, members=None):
        if members is None:
            members = [node_id for label, node_id in self.nodes if label == "Publication"]
        return super().fetch_version_edges(members)


class _MutationTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        self.graph = _MutationGraph()
        self.graph.add("Publication", "W1", title="Graphene transport under magnetic confinement")
        self.graph.add("Publication", "W2", title="Spider populations in coastal habitats")
        self.graph.add("Person", "A1", name_en="Author")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = Settings(data_dir=Path(temporary.name))
        self.enterContext(patch.object(admin_cli, "audited_client", return_value=self.graph))
        self.enterContext(patch.object(graph_repair, "audited_client", return_value=self.graph))
        self.enterContext(patch.object(version_link, "audited_client", return_value=self.graph))

    def decision(self):
        return self.db[overrides.COLLECTION].find_one({"_id": overrides.relationship_override_id(*EDGE)})

    def record(self, op, snapshot=None):
        return overrides.record_relationship_override(self.db, *EDGE, op=op, snapshot=snapshot)

    def assert_manual_claim(self):
        row = self.decision()
        self.assertEqual(row["op"], overrides.LINK)
        self.assertTrue(row["active"])
        self.assertTrue(row["manual_exists"])
        self.assertEqual(row["manual_direction"], ["W2", "W1"])
        self.assertIn(EDGE, overrides.claimed_relationships(self.db))
        self.assertNotIn(EDGE, overrides.tombstoned_relationships(self.db))
        self.assertNotIn(version_repair.FIELD, row)
        self.assertEqual(self.graph.relationships, {EDGE: {}})
        self.assertEqual(self.db[review.COLLECTION].count_documents({}), 0)

    @contextmanager
    def require_graph_lock(self):
        observed = []
        with ExitStack() as stack:
            for name in ("fetch_node_properties", "fetch_node_relationships", "fetch_publications_for_dedup",
                         "fetch_version_edges", "upsert_relationships_batch", "delete_relationships_batch"):
                original = getattr(self.graph, name)

                def checked(*args, _original=original, _name=name, **kwargs):
                    self.assertIsNotNone(locks.holder(self.db, GRAPH), f"{_name} ran outside GRAPH")
                    observed.append(_name)
                    return _original(*args, **kwargs)

                stack.enter_context(patch.object(self.graph, name, side_effect=checked))
            yield observed
        self.assertTrue(observed)
        self.assertIsNone(locks.holder(self.db, GRAPH))

    def checked_record(self, original):
        def record(*args, **kwargs):
            self.assertIsNotNone(locks.holder(self.db, GRAPH), "decision persisted outside GRAPH")
            return original(*args, **kwargs)
        return record


class VersionUiMutationTest(_MutationTest):
    def setUp(self):
        super().setUp()
        create_user(self.db, "editor", "hunter2", role="editor")
        self.app = build(self.config, self.db)
        self.app.dependency_overrides[deps.graph_for] = lambda: self.graph
        self.app.dependency_overrides[deps.graph_if_up] = lambda: self.graph
        self.client = self.enterContext(TestClient(self.app, follow_redirects=False))
        self.client.post("/login", data={"login": "editor", "password": "hunter2"})
        token = self.client.cookies[COOKIE]
        self.csrf = self.db[SESSIONS].find_one({"_id": session_key(token)})["csrf"]

    def mutate(self, action, *, ordinary=False):
        label, node_id, other = ("Person", "A1", "W1") if ordinary else ("Publication", "W2", "W1")
        triple = "Person|AUTHORED|Publication" if ordinary else "outgoing|Publication|IS_VERSION|Publication"
        return self.client.post(f"/nodes/{label}/rel/{action}/{node_id}", data={
            "csrf": self.csrf, "triple": triple, "other_id": other,
        })

    def undo_form(self):
        form = RenderedForms(self.client.get("/overrides").text).with_button(
            "отменить", kind="rel", src_id="W2", target_id="W1",
        )
        self.assertEqual(form["action"], "/overrides/undo-version")
        return form

    def test_version_link_and_unlink_do_not_read_or_write_while_graph_is_busy(self):
        for action in ("add", "delete"):
            with self.subTest(action=action):
                self.graph.relationships = {EDGE: {}} if action == "delete" else {}
                before = deepcopy(self.graph.relationships)
                with locks.held(self.db, GRAPH, "publish"):
                    response = self.mutate(action)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(self.graph.relationships, before)
                self.assertEqual(self.graph.calls, [])
                self.assertIsNone(self.decision())

    def test_ordinary_ui_relationships_keep_working_while_graph_is_busy(self):
        with locks.held(self.db, GRAPH, "publish"):
            self.assertEqual(self.mutate("add", ordinary=True).status_code, 303)
            self.assertIn(("Person", "AUTHORED", "Publication", "A1", "W1"), self.graph.relationships)
            self.assertEqual(self.mutate("delete", ordinary=True).status_code, 303)
            self.assertEqual(locks.holder(self.db, GRAPH)["owner"], "publish")
        self.assertEqual(self.graph.relationships, {})
        self.assertNotIn(version_repair.FIELD, self.db[overrides.COLLECTION].find_one())

    def test_bare_manual_link_delete_and_rendered_undo_preserve_existence_and_direction(self):
        with (
            self.require_graph_lock(),
            patch.object(nodes, "record_relationship_override", side_effect=self.checked_record(
                nodes.record_relationship_override,
            )),
        ):
            self.assertEqual(self.mutate("add").status_code, 303)
            self.assert_manual_claim()
            self.assertEqual(self.mutate("delete").status_code, 303)
            deleted = self.decision()
            self.assertEqual(deleted["op"], overrides.DELETE)
            self.assertTrue(deleted["manual_exists"])
            self.assertEqual(deleted["manual_direction"], ["W2", "W1"])
            self.assertEqual(deleted["snapshot"], {})
            self.assertNotIn(EDGE, self.graph.relationships)
            form = self.undo_form()
            self.assertEqual(self.client.post(form["action"], data=form["fields"]).status_code, 303)
        self.assert_manual_claim()
        self.graph.relationships.clear()
        version_link.run_version_link(self.config, self.db)
        self.assert_manual_claim()

    def test_failed_ui_persistence_holds_graph_through_concurrent_cli_refusal_and_compensation(self):
        def refuse_after_competing_writer(*args, **kwargs):
            self.assertIsNotNone(locks.holder(self.db, GRAPH))
            with self.assertRaises(locks.Busy):
                admin_cli.run(parse_cli("rel", "add", *EDGE, "--once"), self.config, self.db)
            raise AutoReconnect("decision not committed")

        with (
            self.require_graph_lock(),
            patch.object(nodes, "record_relationship_override", side_effect=refuse_after_competing_writer),
        ):
            response = self.mutate("add")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.graph.relationships, {})
        self.assertIsNone(self.decision())

    def test_failed_relink_does_not_delete_the_preexisting_manual_edge(self):
        self.assertEqual(self.mutate("add").status_code, 303)
        before = deepcopy(self.decision())
        self.graph.relationships[EDGE]["note"] = "keep existing metadata"
        with patch.object(nodes, "record_relationship_override", side_effect=AutoReconnect("not committed")):
            self.assertEqual(self.mutate("add").status_code, 503)
        self.assertEqual(self.graph.relationships[EDGE], {"note": "keep existing metadata"})
        self.assertEqual(self.decision(), before)

    def test_version_undo_obeys_the_same_graph_lock_before_changing_the_decision(self):
        before = self.record(overrides.DELETE, snapshot={})
        form = self.undo_form()
        with locks.held(self.db, GRAPH, "reconciler"):
            response = self.client.post(form["action"], data=form["fields"])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.decision()["write_id"], before["write_id"])
        self.assertEqual(self.decision()["op"], overrides.DELETE)
        self.assertEqual(self.graph.calls, [])

    def test_offline_version_undo_commits_a_current_mode_repair_without_losing_the_claim(self):
        self.record(overrides.LINK)
        self.record(overrides.DELETE, snapshot={})
        form = self.undo_form()
        self.app.dependency_overrides[deps.graph_if_up] = lambda: None
        response = self.client.post(form["action"], data=form["fields"])
        self.assertEqual(response.status_code, 303)
        self.assertIn("undone=pending", response.headers["location"])
        row = self.decision()
        self.assertEqual(row["op"], overrides.LINK)
        self.assertTrue(row["active"])
        self.assertTrue(row["manual_exists"])
        self.assertEqual(row["manual_direction"], ["W2", "W1"])
        self.assertEqual(row[version_repair.FIELD]["mode"], "current")
        self.assertEqual(self.graph.relationships, {})


class VersionCliMutationTest(_MutationTest):
    def setUp(self):
        super().setUp()
        self.enterContext(redirect_stdout(StringIO()))

    def run_cli(self, *argv, db=True):
        admin_cli.run(parse_cli(*argv), self.config, self.db if db else None)

    def test_version_add_and_delete_take_graph_even_with_once(self):
        for action in ("add", "delete"):
            for once in (False, True):
                with self.subTest(action=action, once=once):
                    self.graph.relationships = {EDGE: {}} if action == "delete" else {}
                    before = deepcopy(self.graph.relationships)
                    with locks.held(self.db, GRAPH, "ui"), self.assertRaises(locks.Busy):
                        self.run_cli("rel", action, *EDGE, *(["--once"] if once else []))
                    self.assertEqual(self.graph.relationships, before)
                    self.assertEqual(self.graph.calls, [])
                    self.assertIsNone(self.decision())

    def test_once_holds_graph_during_actual_writes_without_creating_a_decision(self):
        with self.require_graph_lock():
            self.run_cli("rel", "add", *EDGE, "--once")
            self.assertEqual(self.graph.relationships, {EDGE: {}})
            self.run_cli("rel", "delete", *EDGE, "--once")
        self.assertEqual(self.graph.relationships, {})
        self.assertIsNone(self.decision())

    def test_version_writes_without_mongo_are_refused_even_with_once(self):
        for action in ("add", "delete"):
            with self.subTest(action=action), self.assertRaisesRegex(SystemExit, "MongoDB.*GRAPH"):
                self.run_cli("rel", action, *EDGE, "--once", db=False)
        self.assertEqual(self.graph.calls, [])

    def test_ordinary_cli_relationships_do_not_take_the_version_lock(self):
        ordinary = ("Person", "AUTHORED", "Publication", "A1", "W1")
        with locks.held(self.db, GRAPH, "ui"):
            self.run_cli("rel", "add", *ordinary)
            self.run_cli("rel", "delete", *ordinary)
            self.assertEqual(locks.holder(self.db, GRAPH)["owner"], "ui")
        self.assertNotIn(version_repair.FIELD, self.db[overrides.COLLECTION].find_one())
        self.run_cli("rel", "add", *ordinary, "--once", db=False)
        self.assertIn(ordinary, self.graph.relationships)

    def test_cli_bare_link_delete_and_undo_preserve_the_manual_claim(self):
        with (
            self.require_graph_lock(),
            patch.object(admin_cli, "record_relationship_override", side_effect=self.checked_record(
                admin_cli.record_relationship_override,
            )),
        ):
            self.run_cli("rel", "add", *EDGE)
            self.assert_manual_claim()
            self.run_cli("rel", "delete", *EDGE)
            self.assertEqual(self.decision()["snapshot"], {})
            self.assertTrue(self.decision()["manual_exists"])
            self.run_cli("overrides", "undo-rel", *EDGE)
        self.assert_manual_claim()

    def test_cli_undo_rel_and_standalone_apply_contend_for_the_shared_graph_lock(self):
        before = self.record(overrides.DELETE, snapshot={})
        for command in (("overrides", "undo-rel", *EDGE), ("overrides", "apply")):
            with self.subTest(command=command):
                with locks.held(self.db, GRAPH, "ui"), self.assertRaises(locks.Busy):
                    self.run_cli(*command)
                self.assertEqual(self.decision()["write_id"], before["write_id"])
                self.assertEqual(self.graph.calls, [])


class VersionRepairIntentStorageTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        self.moment = datetime(2026, 11, 20, 10, tzinfo=UTC)
        self.enterContext(patch.object(version_repair, "_now", return_value=self.moment))

    def save(self, intent=None, *, collection="graph_overrides", document_id="source"):
        intent = intent or version_repair.make_intent(["W2", "W1"])
        self.db[collection].update_one({"_id": document_id}, {"$set": {
            "decision": "preserved", version_repair.FIELD: intent,
        }}, upsert=True)
        return intent

    def test_make_intent_defaults_to_current_sorts_members_and_uses_unique_tokens(self):
        first = version_repair.make_intent(["W2", "W1"])
        second = version_repair.make_intent(["W1", "W2"], mode="reopen")
        self.assertEqual(first["mode"], "current")
        self.assertEqual(second["mode"], "reopen")
        self.assertEqual(first["members"], ["W1", "W2"])
        self.assertEqual(first["requested_at"], self.moment)
        self.assertEqual(first["next_attempt_at"], self.moment)
        self.assertNotEqual(first["token"], second["token"])

    def test_invalid_pair_shapes_members_and_modes_are_rejected(self):
        for members in ([], ["W1"], ["W1", "W2", "W3"], ["W1", "W1"], ["W1", ""], ["W1", 2]):
            with self.subTest(members=members), self.assertRaises(ValueError):
                version_repair.make_intent(members)
        with self.assertRaises(ValueError):
            version_repair.make_intent(["W1", "W2"], mode="replay")

    def test_pending_scans_only_the_two_source_collections_and_filters_due_times(self):
        first = self.save()
        future = version_repair.make_intent(["W1", "W2"])
        future["next_attempt_at"] = self.moment + timedelta(seconds=60)
        second = self.save(future, collection="review_pairs", document_id="question")
        self.save(collection="jobs", document_id="unrelated")
        self.assertCountEqual([(name, key, intent["token"]) for name, key, intent in version_repair.pending(self.db)], [
            ("graph_overrides", "source", first["token"]), ("review_pairs", "question", second["token"]),
        ])
        self.assertEqual([(name, key) for name, key, _ in version_repair.pending(self.db, due_only=True)], [
            ("graph_overrides", "source"),
        ])

    def test_load_and_ack_require_the_current_generation_and_preserve_the_decision(self):
        old = self.save()
        newer = self.save()
        self.assertIsNone(version_repair.load(self.db, "graph_overrides", "source", old["token"]))
        self.assertFalse(version_repair.ack(self.db, "graph_overrides", "source", old["token"]))
        self.assertEqual(version_repair.load(self.db, "graph_overrides", "source", newer["token"])["token"], newer["token"])
        self.assertTrue(version_repair.ack(self.db, "graph_overrides", "source", newer["token"]))
        row = self.db.graph_overrides.find_one({"_id": "source"})
        self.assertEqual(row["decision"], "preserved")
        self.assertNotIn(version_repair.FIELD, row)
        self.assertEqual(row["version_repair_completed"]["token"], newer["token"])
        self.assertFalse(version_repair.ack(self.db, "graph_overrides", "source", newer["token"]))

    def test_defer_is_token_guarded_records_errors_and_never_acknowledges(self):
        old = self.save()
        newer = self.save()
        self.assertFalse(version_repair.defer(self.db, "graph_overrides", "source", old["token"], error="obsolete"))
        self.assertTrue(version_repair.defer(self.db, "graph_overrides", "source", newer["token"],
                                             error="graph unavailable", delay_seconds=90))
        row = self.db.graph_overrides.find_one({"_id": "source"})
        intent = row[version_repair.FIELD]
        self.assertEqual(row["decision"], "preserved")
        self.assertEqual(intent["token"], newer["token"])
        self.assertEqual(intent["mode"], "current")
        self.assertEqual(intent["members"], ["W1", "W2"])
        self.assertEqual(intent["last_error"], "graph unavailable")
        self.assertEqual(intent["failures"], 1)
        self.assertEqual(intent["next_attempt_at"], (self.moment + timedelta(seconds=90)).replace(tzinfo=None))
        self.assertNotIn("version_repair_completed", row)

    def test_defer_bounds_error_size_and_enforces_a_positive_delay(self):
        intent = self.save()
        version_repair.defer(self.db, "graph_overrides", "source", intent["token"], error="x" * 2500, delay_seconds=0)
        saved = version_repair.load(self.db, "graph_overrides", "source", intent["token"])
        self.assertEqual(len(saved["last_error"]), 2000)
        self.assertEqual(saved["next_attempt_at"], (self.moment + timedelta(seconds=1)).replace(tzinfo=None))
        version_repair.defer(self.db, "graph_overrides", "source", intent["token"])
        self.assertEqual(version_repair.load(self.db, "graph_overrides", "source", intent["token"])["failures"], 1)

    def test_load_ack_and_defer_reject_unrelated_collections(self):
        for operation in (version_repair.load, version_repair.ack, version_repair.defer):
            with self.subTest(operation=operation.__name__), self.assertRaises(ValueError):
                operation(self.db, "jobs", "source", "token")
        self.assertEqual(self.db.list_collection_names(), [])


class VersionRepairProjectionTest(_MutationTest):
    def prepare_withdrawal(self, mode):
        report = {"status": "held", "entity": "publication", "record_a": "W1", "record_b": "W2",
                  "bucket": "STRONG_FUZZY", "mergeable": True}
        review.record_held(self.db, [report], source=review.GRAPH)
        review.record_verdict(self.db, review.PUB_VERSION, ["W1", "W2"], review.SAME)
        self.assertTrue(review.withdraw(self.db, review.PUB_VERSION, ["W1", "W2"]))
        row = self.db[review.COLLECTION].find_one()
        intent = version_repair.make_intent(["W1", "W2"], mode=mode)
        self.db[review.COLLECTION].update_one({"_id": row["_id"]}, {"$set": {version_repair.FIELD: intent}})
        self.graph.relationships[EDGE] = {
            "bucket": "STRONG_FUZZY", "mergeable": True, "status": "same", "confirmation_source": "auto",
        }
        return row["_id"], intent

    def run_repair(self, document_id, intent):
        return graph_repair.run_pending_version_repair(
            self.config, self.db, "review_pairs", document_id, intent["token"],
        )

    def test_current_mode_recomputes_and_removes_an_obsolete_automatic_edge(self):
        document_id, intent = self.prepare_withdrawal("current")
        with self.require_graph_lock():
            result = self.run_repair(document_id, intent)
        self.assertEqual(self.graph.relationships, {})
        self.assertEqual(result["version_repairs"], 1)
        self.assertIsNone(version_repair.load(self.db, "review_pairs", document_id, intent["token"]))

    def test_reopen_mode_demotes_old_confirmation_without_restoring_automatic_authority(self):
        document_id, intent = self.prepare_withdrawal("reopen")
        with self.require_graph_lock():
            result = self.run_repair(document_id, intent)
        self.assertEqual(set(self.graph.relationships), {EDGE})
        edge = self.graph.relationships[EDGE]
        self.assertEqual(edge["status"], "held")
        self.assertNotIn("confirmation_source", edge)
        self.assertNotIn("confirmation_policy", edge)
        self.assertEqual(result["version_repairs"], 1)
        self.assertIsNone(version_repair.load(self.db, "review_pairs", document_id, intent["token"]))

    def test_a_question_sync_failure_keeps_the_intent_and_retries_without_false_confirmation(self):
        document_id, intent = self.prepare_withdrawal("reopen")
        with (
            patch.object(review, "record_held", side_effect=AutoReconnect("question sync failed")),
            self.assertRaises(AutoReconnect),
        ):
            self.run_repair(document_id, intent)
        pending = version_repair.load(self.db, "review_pairs", document_id, intent["token"])
        self.assertIn("question sync failed", pending["last_error"])
        self.assertEqual(pending["failures"], 1)
        self.assertEqual(self.graph.relationships[EDGE]["status"], "held")
        self.assertEqual(self.run_repair(document_id, intent)["version_repairs"], 1)
        self.assertNotIn("confirmation_source", self.graph.relationships[EDGE])

    def test_a_late_old_generation_does_not_open_a_graph_client_or_ack_newer_work(self):
        document_id, old = self.prepare_withdrawal("reopen")
        newer = version_repair.make_intent(["W1", "W2"])
        self.db[review.COLLECTION].update_one({"_id": document_id}, {"$set": {version_repair.FIELD: newer}})
        with patch.object(graph_repair, "audited_client") as client:
            self.assertEqual(self.run_repair(document_id, old), {"version_repairs_obsolete": 1})
        client.assert_not_called()
        self.assertIsNotNone(version_repair.load(self.db, "review_pairs", document_id, newer["token"]))

    def test_legacy_active_link_proves_manual_origin_but_legacy_delete_does_not(self):
        for prior_op, expected in ((overrides.LINK, True), (overrides.DELETE, False)):
            with self.subTest(prior_op=prior_op):
                self.db[overrides.COLLECTION].delete_many({})
                self.db[overrides.COLLECTION].insert_one({
                    "_id": overrides.relationship_override_id(*EDGE), "kind": "rel", "src_label": VERSION[0],
                    "rel_type": VERSION[1], "tgt_label": VERSION[2], "src_id": "W2", "target_id": "W1",
                    "active": True, "op": prior_op, "snapshot": {},
                })
                deleted = self.record(overrides.DELETE, snapshot={})
                self.assertEqual(deleted["manual_exists"], expected)
                self.assertTrue(overrides.deactivate_relationship_override(
                    self.db, *EDGE, only_op=overrides.DELETE, expected_write_id=deleted["write_id"],
                ))
                row = self.decision()
                self.assertEqual(row["active"], expected)
                self.assertEqual(row["op"], overrides.LINK if expected else overrides.DELETE)
                self.assertEqual(row[version_repair.FIELD]["mode"], "current")
                if expected:
                    self.assertEqual(row["manual_direction"], ["W2", "W1"])
                else:
                    self.assertNotIn("manual_direction", row)
