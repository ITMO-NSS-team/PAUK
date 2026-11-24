"""Graph snapshot exports coordinate with other graph operations."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import mongomock

from pauk.jobs.locks import Busy, held, holder
from pauk.jobs.models import GRAPH
from pauk.ops.config import Config
from pauk.ops.engine import Engine
from pauk.ops.store import Store


class PrepareDataGraphLockTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        config = Config(
            home=root / "home", essdata=root / "essdata", stand="test",
            env_file=root / "env", state=root / "state", assets=root / "web",
        )
        self.db = mongomock.MongoClient()["pauk_test"]
        self.store = Store(config.state)
        self.engine = Engine(config, self.store, self.db)
        self.engine.stage = Mock()
        self.engine.busy = Mock(return_value=False)

    def test_fresh_export_holds_lock_only_until_export_finishes(self):
        def command(args):
            if "export" in args:
                self.assertTrue(holder(self.db, GRAPH)["owner"].startswith("ops-export-"))
                with self.assertRaises(Busy), held(self.db, GRAPH, owner="publisher"):
                    pass
            else:
                self.assertIsNone(holder(self.db, GRAPH))

        self.engine.command = Mock(side_effect=command)
        self.engine.prepare_data("release")
        self.assertEqual(self.engine.command.call_count, 2)
        self.assertIsNone(holder(self.db, GRAPH))

    def test_export_failure_releases_lock(self):
        self.engine.command = Mock(side_effect=RuntimeError("export failed"))
        with self.assertRaisesRegex(RuntimeError, "export failed"):
            self.engine.prepare_data("release")
        self.assertIsNone(holder(self.db, GRAPH))
        with held(self.db, GRAPH, owner="publisher"):
            pass
        self.assertIsNone(self.store.get("last-snapshot"))

    def test_long_export_renews_lease(self):
        renewed = threading.Event()

        def renew_lock(db, resource, owner):
            self.assertEqual(holder(db, resource)["owner"], owner)
            renewed.set()
            return True

        def command(args):
            if "export" in args:
                self.assertTrue(renewed.wait(2))

        self.engine.command = Mock(side_effect=command)
        with patch("pauk.ops.engine.GRAPH_RENEW_SECONDS", 0.01), patch(
            "pauk.ops.engine.renew", side_effect=renew_lock
        ):
            self.engine.prepare_data("release")
        self.assertIsNone(holder(self.db, GRAPH))

    def test_failed_renewal_rejects_snapshot(self):
        attempted = threading.Event()

        def renew_lock(*args):
            attempted.set()
            return False

        self.engine.command = Mock(side_effect=lambda args: attempted.wait(2))
        with patch("pauk.ops.engine.GRAPH_RENEW_SECONDS", 0.01), patch(
            "pauk.ops.engine.renew", side_effect=renew_lock
        ), self.assertRaisesRegex(RuntimeError, "lock renewal failed"):
            self.engine.prepare_data("release")
        self.assertIsNone(self.store.get("last-snapshot"))

    def test_reuse_does_not_need_graph_lock(self):
        snapshots = self.engine.config.essdata / "snapshots" / self.engine.config.stand
        snapshots.mkdir(parents=True)
        (snapshots / "saved.json").write_text("{}")
        self.store.set("last-snapshot", "saved.json")
        self.engine.command = Mock()
        with held(self.db, GRAPH, owner="publisher"):
            _, snapshot = self.engine.prepare_data("release", fresh=False)
            self.assertEqual(holder(self.db, GRAPH)["owner"], "publisher")
        self.assertEqual(snapshot, "saved.json")
        self.engine.command.assert_called_once()

    def test_fresh_export_fails_when_graph_is_locked(self):
        self.engine.command = Mock()
        with held(self.db, GRAPH, owner="publisher"), self.assertRaises(Busy):
            self.engine.prepare_data("release")
        self.engine.command.assert_not_called()
