"""Recovery of site slots and failed release transitions."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pauk.ops.config import Config
from pauk.ops.engine import Engine, write_json
from pauk.ops.status import observed
from pauk.ops.store import Store


class SiteStateTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        config = Config(home=root / "home", essdata=root / "data", stand="test", env_file=root / "env", state=root / "state", assets=root / "web")
        self.store = Store(config.state)
        self.engine = Engine(config, self.store, None)
        for name in ("old", "new"):
            release = config.releases / name
            (release / "site").mkdir(parents=True)
            (release / "site/index.html").write_text(name)
            write_json(release / "release.json", {"id": name})
        self.engine.http_check = Mock()

    def test_publish_rolls_back_when_state_write_fails(self):
        self.engine.mount_slot("main", "old")
        self.store.set("live", "old")
        original = self.store.set_many

        def fail_commit(values):
            if values.get("live") == "new":
                raise OSError("disk full")
            original(values)

        with patch.object(self.store, "set_many", side_effect=fail_commit), self.assertRaisesRegex(
            OSError, "disk full"
        ):
            self.engine.publish("new")
        self.assertEqual((self.engine.config.site / "main").readlink(), Path("releases/old"))
        self.assertEqual(self.store.get("live"), "old")

    def test_publication_repairs_partial_copy_before_switching(self):
        self.engine.mount_slot("main", "old")
        self.store.set("live", "old")
        source = self.engine.config.releases / "new/site"
        (source / "authors-detail.json").write_text('{"author": "complete"}')
        destination = self.engine.config.site / "releases/new"
        destination.mkdir()
        (destination / "index.html").write_text("incomplete")

        def check(port):
            self.assertEqual(port, 8520)
            self.assertEqual((self.engine.config.site / "main/index.html").read_text(), "new")
            self.assertEqual((self.engine.config.site / "main/authors-detail.json").read_text(),
                             '{"author": "complete"}')

        self.engine.http_check.side_effect = check
        self.engine.publish("new")
        self.assertEqual(self.store.get("live"), "new")

    def test_failed_copy_keeps_published_site_and_state(self):
        self.engine.mount_slot("main", "old")
        self.store.set("live", "old")
        with patch("pauk.ops.site_files.shutil.copytree", side_effect=OSError("disk full")), \
                self.assertRaisesRegex(OSError, "disk full"):
            self.engine.publish("new")
        self.assertEqual((self.engine.config.site / "main/index.html").read_text(), "old")
        self.assertEqual(self.store.get("live"), "old")
        self.engine.http_check.assert_called_once_with(8520)

    def test_publish_restores_state_even_if_write_committed_before_error(self):
        self.engine.mount_slot("main", "old")
        self.store.set("live", "old")
        original = self.store.set_many

        def write_then_fail(values):
            original(values)
            if values.get("live") == "new":
                raise OSError("late failure")

        with patch.object(self.store, "set_many", side_effect=write_then_fail), self.assertRaises(OSError):
            self.engine.publish("new")
        self.assertEqual((self.engine.config.site / "main").readlink(), Path("releases/old"))
        self.assertEqual(self.store.get("live"), "old")

    def test_restart_reconciles_all_slots_without_http(self):
        self.store.set("live", "old")
        self.store.set("preview", "old")
        self.store.set("archive-8522", "old")
        self.engine.mount_slot("main", "new")
        self.engine.mount_slot("preview", "new")
        self.engine.mount_slot("archive-8523", "old")
        fresh = Engine(self.engine.config, Store(self.engine.config.state), None)
        fresh.stop.set()
        fresh.start()
        fresh.thread.join()
        self.assertIsNone(fresh.site_state_error)
        self.assertEqual(self.store.get("live"), "new")
        self.assertEqual(self.store.get("preview"), "new")
        self.assertIsNone(self.store.get("archive-8522"))
        self.assertEqual(self.store.get("archive-8523"), "old")
        self.engine.http_check.assert_not_called()

    def test_corrupt_reference_is_reported_and_not_recorded(self):
        self.engine.config.site.mkdir(parents=True)
        (self.engine.config.site / "main").symlink_to("releases/missing")
        self.store.set("live", "old")
        self.engine.reconcile_site()
        self.assertIn("main", self.engine.site_state_error)
        self.assertIsNone(self.store.get("live"))
        with self.assertRaises(RuntimeError):
            self.engine.execute("publish", {"id": "new"})

    def test_invalid_metadata_does_not_crash_recovery(self):
        self.engine.mount_slot("main", "old")
        (self.engine.config.releases / "old/release.json").write_text("null")
        self.engine.reconcile_site()
        self.assertIn("main", self.engine.site_state_error)
        self.assertIsNone(self.store.get("live"))
        self.assertEqual([release["id"] for release in self.engine.releases()], ["new"])

    def test_broken_slot_keeps_diagnostics_and_backups_available(self):
        self.engine.config.site.mkdir(parents=True)
        (self.engine.config.site / "main").symlink_to("releases/missing")
        self.store.set("live", "old")
        self.engine.reconcile_site()
        with patch.object(self.engine, "inspect", return_value=[]), \
                patch("pauk.ops.status.subprocess.run", return_value=Mock(stdout="")), \
                patch("pauk.ops.status.GraphDatabase.driver", side_effect=ConnectionError), \
                patch("pauk.ops.status.requests.get", side_effect=ConnectionError):
            state = observed(self.engine)
        self.assertIn("main", state["error"])
        self.assertIsNone(state["live"])
        self.assertIsNone(state["preview"])
        with patch.object(self.engine, "backup") as backup:
            self.engine.execute("create-backup", {"name": "manual"})
        backup.assert_called_once_with("manual")

    def test_archive_start_and_stop_rollback_on_save_error(self):
        with patch.object(self.store, "set", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.engine.execute("start-archive", {"id": "new", "port": 8522})
        self.assertFalse((self.engine.config.site / "archive-8522").is_symlink())
        self.engine.execute("start-archive", {"id": "new", "port": 8522})
        with patch.object(self.store, "set", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.engine.execute("stop-archive", {"id": "new"})
        self.assertEqual((self.engine.config.site / "archive-8522").readlink(), Path("releases/new"))
        self.assertEqual(self.store.get("archive-8522"), "new")

    def test_preview_cleanup_rolls_back_on_save_error(self):
        self.engine.mount_slot("preview", "new")
        self.store.set("preview", "new")
        with patch.object(self.store, "set", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.engine.execute("discard-preview", {})
        self.assertEqual((self.engine.config.site / "preview").readlink(), Path("releases/new"))
        self.assertEqual(self.store.get("preview"), "new")

    def test_legacy_import_interruption_restores_original_directory(self):
        main = self.engine.config.site / "main"
        main.mkdir(parents=True)
        (main / "index.html").write_text("legacy")
        backup = main.parent / ".previous-main-imported-12345678"
        main.rename(backup)
        self.store.set("live", "imported-12345678")
        self.engine.reconcile_site()
        self.assertEqual((main / "index.html").read_text(), "legacy")
        self.assertIsNone(self.store.get("live"))
        self.assertIsNone(self.engine.site_state_error)

    def test_failed_rollback_blocks_site_actions(self):
        self.engine.mount_slot("main", "old")
        self.store.set("live", "old")
        mount = self.engine.mount_slot

        def fail_restore(slot, release):
            if release == "old":
                raise OSError("restore failed")
            mount(slot, release)

        with patch.object(self.engine, "mount_slot", side_effect=fail_restore), patch.object(
            self.store, "set_many", side_effect=OSError("write failed")
        ), self.assertRaisesRegex(RuntimeError, "restore failed"):
            self.engine.publish("new")
        self.assertIn("restore failed", self.engine.site_state_error)
        with self.assertRaises(RuntimeError):
            self.engine.execute("publish", {"id": "old"})
        self.engine.reconcile_site()
        self.assertEqual(self.store.get("live"), "old")
        self.assertIsNone(self.engine.site_state_error)

    def test_interrupted_switch_returns_previous_release_on_restart(self):
        for slot, key, port in (("main", "live", 8520), ("preview", "preview", None),
                                ("archive-8522", "archive-8522", 8522)):
            with self.subTest(slot=slot):
                self.engine.mount_slot(slot, "old")
                self.store.set(key, "old")
                with patch.object(self.store, "set_many", side_effect=KeyboardInterrupt), \
                        self.assertRaises(KeyboardInterrupt):
                    self.engine.switch_slot(slot, key, "new", port)
                self.assertEqual((self.engine.config.site / slot).readlink(), Path("releases/new"))
                fresh = Engine(self.engine.config, Store(self.engine.config.state), None)
                with patch.object(fresh, "http_check"):
                    fresh.reconcile_site()
                self.assertEqual((fresh.config.site / slot).readlink(), Path("releases/old"))
                self.assertEqual(self.store.get(key), "old")
                self.assertIsNone(self.store.get("slot-switch"))

    def test_failed_recovery_remains_pending_across_restarts(self):
        self.engine.mount_slot("main", "old")
        self.store.set("live", "old")
        with patch.object(self.engine, "http_check", side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            self.engine.publish("new")
        fresh = Engine(self.engine.config, Store(self.engine.config.state), None)
        with patch.object(fresh, "http_check", side_effect=RuntimeError("nginx unavailable")):
            fresh.reconcile_site()
        self.assertIsNotNone(self.store.get("slot-switch"))
        self.assertIn("nginx unavailable", fresh.site_state_error)
        with patch.object(fresh, "http_check"):
            fresh.reconcile_site()
        self.assertIsNone(self.store.get("slot-switch"))
        self.assertEqual(self.store.get("live"), "old")

    def test_committed_switch_survives_restart(self):
        self.engine.publish("new")
        fresh = Engine(self.engine.config, Store(self.engine.config.state), None)
        fresh.reconcile_site()
        self.assertEqual(self.store.get("live"), "new")
        self.assertIsNone(self.store.get("slot-switch"))

    def test_http_check_rejects_html_fallback_for_detail(self):
        del self.engine.http_check
        valid = Mock()
        invalid = Mock()
        invalid.json.side_effect = json.JSONDecodeError("invalid", "html", 0)
        with patch("pauk.ops.engine.requests.get", side_effect=[valid, invalid]) as get, self.assertRaises(
            json.JSONDecodeError
        ):
            self.engine.http_check(8520)
        self.assertIn("authors-detail.json", get.call_args.args[0])
