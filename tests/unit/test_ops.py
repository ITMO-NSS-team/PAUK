"""Control-plane durability, anonymous access and failure handling."""

import gzip
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import mongomock
from fastapi.testclient import TestClient

from pauk.ops.app import build
from pauk.ops.config import Config
from pauk.ops.engine import FILES, Engine, checksum, compress_assets, directory, write_json
from pauk.ops.status import published_ports
from pauk.ops.store import Store
from pauk.settings import Settings


class OperationsFixture:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = Config(
            home=self.root / "home",
            essdata=self.root / "essdata",
            stand="test",
            env_file=self.root / "env",
            state=self.root / "state",
            assets=self.root / "web",
        )
        self.db = mongomock.MongoClient()["pauk_test"]
        self.store = Store(self.config.state)
        self.engine = Engine(self.config, self.store, self.db)

    def queued(self, action="build-site"):
        return self.store.enqueue(dict(action=action, title="Operation", kind="Test", args={}), "tester")

    def backup(self, name="backup1"):
        path = self.config.dumps / name
        path.mkdir(parents=True)
        with gzip.open(path / FILES[0], "wb") as stream:
            stream.write(b"test archive")
        for file in FILES[1:]:
            (path / file).write_bytes(b"dump")
        manifest = dict(
            id=name,
            name=name,
            created="2026-01-01T00:00:00+00:00",
            verified=False,
            restored=False,
            files={file: checksum(path / file) for file in FILES},
            credentials=self.engine.credentials(),
        )
        write_json(path / "manifest.json", manifest)
        return path

    def release(self, name):
        root = self.config.releases / name
        (root / "site").mkdir(parents=True)
        (root / "site/index.html").write_text(name)
        write_json(
            root / "release.json", dict(id=name, commit="abc123", branch="test", created="now", snapshot="test")
        )
        return root


class OperationsTest(OperationsFixture, unittest.TestCase):
    def test_precompressed_assets_preserve_bytes(self):
        root = self.root / "site"
        root.mkdir()
        content = '{"label":"Проверка"}'.encode()
        (root / "graph-data.json").write_bytes(content)
        (root / "image.png").write_bytes(b"image")
        compress_assets(root)
        self.assertEqual(gzip.decompress((root / "graph-data.json.gz").read_bytes()), content)
        self.assertEqual((root / "graph-data.json").read_bytes(), content)
        self.assertFalse((root / "image.png.gz").exists())

    def test_queue_admission_is_atomic(self):
        def enqueue(_):
            try:
                self.queued()
                return True
            except ValueError:
                return False

        with ThreadPoolExecutor(2) as executor:
            self.assertEqual(sum(executor.map(enqueue, range(2))), 1)

    def test_failed_operations_remain_failed_after_restart(self):
        operation = self.queued("restore-backup")
        operation["state"] = "running"
        self.store.save(operation)
        reopened = Store(self.config.state)
        reopened.recover()
        self.assertEqual(reopened.operations()[0]["state"], "failed")
        self.assertEqual(reopened.operations()[0]["action"], "restore-backup")
        self.assertFalse(self.engine.run_once())

    def test_recording_progress_preserves_creation_order(self):
        first = self.queued()
        first["state"] = "done"
        self.store.save(first)
        second = self.queued()
        self.store.save(first)
        self.assertEqual([row["id"] for row in self.store.operations()], [second["id"], first["id"]])

    def test_no_paths_escape_the_backup_root(self):
        for value in ("../private", "/etc/passwd", "--help", "a/b"):
            with self.assertRaises(ValueError):
                directory(self.config.dumps, value)
        self.config.dumps.mkdir(parents=True)
        (self.config.dumps / "link").symlink_to(self.root)
        with self.assertRaises(ValueError):
            directory(self.config.dumps, "link")

    def test_tampered_backup_refused_before_maintenance(self):
        path = self.backup()
        (path / FILES[1]).write_text("corrupt")
        with patch.object(self.engine, "maintenance") as maintenance:
            with self.assertRaisesRegex(ValueError, "контрольная сумма"):
                self.engine.restore("backup1")
            maintenance.assert_not_called()

    def test_failed_dump_restarts_only_previously_running_services(self):
        services = [dict(Service=service, State="running") for service in ("mongo", "neo4j", "worker")]
        calls = []

        def command(args, **kwargs):
            calls.append(args)

        with (
            patch.object(self.engine, "inspect", return_value=services),
            patch.object(self.engine, "command", command),
            self.assertRaisesRegex(RuntimeError, "dump failed"),
            self.engine.maintenance(),
        ):
            raise RuntimeError("dump failed")
        starts = [args for args in calls if "start" in args]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0][-3:], ["start", "neo4j", "worker"])
        self.assertNotIn("admin", starts[0])

    def test_busy_worker_prevents_maintenance(self):
        self.db.jobs.insert_one({"state": "running"})
        with patch.object(self.engine, "command") as command:
            with self.assertRaisesRegex(RuntimeError, "Воркер занят"), self.engine.maintenance():
                pass
            command.assert_not_called()

    def test_restore_refused_when_credentials_changed(self):
        self.backup()
        with (
            patch.object(self.engine, "command"),
            patch.object(self.engine, "credentials", return_value="new"),
            self.assertRaisesRegex(ValueError, "Учётные данные"),
        ):
            self.engine.restore("backup1")

    def test_restore_requires_successful_safety_backup(self):
        self.backup()
        with (
            patch.object(self.engine, "verify"),
            patch.object(self.engine, "backup", side_effect=RuntimeError("no space")),
            patch.object(self.engine, "maintenance") as maintenance,
        ):
            with self.assertRaisesRegex(RuntimeError, "no space"):
                self.engine.restore("backup1")
            maintenance.assert_not_called()

    def test_restore_refuses_changed_application_credentials(self):
        self.backup()
        with (
            patch.dict(os.environ, {"MONGO_URI": "mongodb://changed-application-credentials"}),
            patch.object(self.engine, "command"),
            self.assertRaisesRegex(ValueError, "Учётные данные"),
        ):
            self.engine.restore("backup1")

    def test_legacy_archives_are_visible_without_restore_permissions(self):
        self.backup()
        old = self.config.dumps / "before-compose"
        old.mkdir()
        (old / "mongo.archive.gz").write_bytes(b"mongo")
        (old / "neo4j.dump").write_bytes(b"neo4j")
        (old / "system.dump").write_bytes(b"system")
        (old / "dump.log").write_text("not a backup")
        exported = self.config.dumps / "collections"
        exported.mkdir()
        (exported / "repositories.jsonl").write_text("{}")
        empty = self.config.dumps / "notes"
        empty.mkdir()
        (empty / "README.md").write_text("notes")
        (self.config.dumps / "external").symlink_to(old)
        rows = {row["id"]: row for row in self.engine.backups()}
        self.assertEqual(set(rows), {"backup1", "before-compose", "collections"})
        self.assertTrue(rows["backup1"]["managed"])
        self.assertFalse(rows["before-compose"]["managed"])
        self.assertEqual(set(rows["before-compose"]["components"]), {"MongoDB", "Neo4j", "system"})
        self.assertEqual(rows["collections"]["components"], ["экспорт MongoDB"])
        self.assertFalse((old / "manifest.json").exists())
        with self.assertRaisesRegex(ValueError, "Последнюю копию"):
            self.engine.execute("delete-backup", {"id": "backup1"})

    def test_host_ports_use_observed_publishers_and_collapse_ranges(self):
        publishers = [{"PublishedPort": port} for port in [8520, 8521, 8522, 8522, 8506, 0]]
        self.assertEqual(published_ports({"Publishers": publishers}), "8506, 8520-8522")
        self.assertEqual(published_ports({"Publishers": None}), "—")
        self.assertEqual(published_ports({}), "—")

    def test_last_backup_cannot_be_deleted(self):
        path = self.backup()
        with self.assertRaisesRegex(ValueError, "Последнюю"):
            self.engine.execute("delete-backup", {"id": "backup1"})
        self.assertTrue(path.is_dir())

    def test_failed_http_check_rolls_live_release_back(self):
        self.release("old")
        self.release("new")
        self.engine.mount_slot("main", "old")
        self.store.set("live", "old")
        with (
            patch.object(self.engine, "http_check", side_effect=RuntimeError("HTTP failed")),
            self.assertRaises(RuntimeError),
        ):
            self.engine.publish("new")
        self.assertEqual((self.config.site / "main/index.html").read_text(), "old")
        self.assertEqual(self.store.get("live"), "old")

    def test_first_publication_preserves_existing_site(self):
        self.release("new")
        main = self.config.site / "main"
        main.mkdir(parents=True)
        (main / "index.html").write_text("existing")
        with (
            patch.object(self.engine, "http_check", side_effect=RuntimeError("HTTP failed")),
            self.assertRaises(RuntimeError),
        ):
            self.engine.publish("new")
        self.assertEqual((main / "index.html").read_text(), "existing")
        self.assertTrue(self.store.get("live").startswith("imported-"))

    def test_precreated_empty_slot_is_initialized_without_deleting_content(self):
        self.release("new")
        slot = self.config.site / "preview"
        slot.mkdir(parents=True)
        self.engine.mount_slot("preview", "new")
        self.assertTrue(slot.is_symlink())
        self.assertEqual((slot / "index.html").read_text(), "new")

    def test_nonempty_slot_is_preserved(self):
        self.release("new")
        slot = self.config.site / "preview"
        slot.mkdir(parents=True)
        (slot / "keep.txt").write_text("keep")
        with self.assertRaises(RuntimeError):
            self.engine.mount_slot("preview", "new")
        self.assertEqual((slot / "keep.txt").read_text(), "keep")

    def test_pages_reuses_public_data_of_the_published_release(self):
        release = self.release("current")
        (release / "public").mkdir()
        script = self.root / "publish-pages"
        script.touch()
        self.engine.config = replace(self.config, pages_script=script)
        self.store.set("live", "current")

        def command(args, **kwargs):
            self.assertEqual(kwargs["extra_env"]["PAUK_PUBLIC_DIR"], str(release / "public"))
            Path(kwargs["extra_env"]["PAUK_PAGES_RESULT"]).write_text(
                json.dumps(dict(conclusion="success", headBranch="main", headSha="abc123456789"))
            )

        with (
            patch.object(self.engine, "prepare_data") as prepare,
            patch.object(self.engine, "command", command),
            patch("pauk.ops.engine.requests.get"),
        ):
            self.engine.execute("publish-pages", {})
            prepare.assert_not_called()
        self.assertEqual(self.store.get("publicRelease")["commit"], "abc123456789")

    def test_operation_failure_and_output_survive_runner_recreation(self):
        self.queued()
        with patch.object(self.engine, "execute", side_effect=RuntimeError("failed build")):
            self.assertTrue(self.engine.run_once())
        row = Store(self.config.state).operations()[0]
        self.assertEqual(row["state"], "failed")
        self.assertIn("failed build", row["logs"][-1])


class OperationsApiTest(OperationsFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app = build(Settings(), self.db, self.config)
        self.client = TestClient(self.app, follow_redirects=False)

    def test_endpoints_are_open_without_graph_editor_routes(self):
        self.assertEqual(self.client.get("/api/ops/operations").status_code, 200)
        for path in ("/jobs", "/login", "/logout"):
            self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.db.admin_sessions.count_documents({}), 0)

    def test_anonymous_writes_still_require_explicit_confirmation(self):
        payload = {"action": "restore-backup", "id": "backup1"}
        self.assertEqual(self.client.post("/api/ops/commands", json=payload).status_code, 422)
        payload["confirmation"] = "ВОССТАНОВИТЬ test"
        response = self.client.post("/api/ops/commands", json=payload)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["actor"], "anonymous")
        self.assertFalse(self.client.cookies)

    def test_command_and_schedule_validation(self):
        for payload in (
            {"action": "shell"},
            {"action": "verify-backup", "id": "../secret"},
            {"action": "start-archive", "port": 9000},
        ):
            self.assertEqual(self.client.post("/api/ops/commands", json=payload).status_code, 422)
        self.assertEqual(
            self.client.put(
                "/api/ops/schedule", json={"enabled": True, "time": "25:00", "keep": 0}
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.put(
                "/api/ops/schedule", json={"enabled": True, "time": "03:00", "keep": 3}
            ).status_code,
            200,
        )
        self.assertTrue(Store(self.config.state).get("schedule")["enabled"])

    def test_state_and_settings_remain_available_without_mongo(self):
        database = MagicMock()
        database.client.admin.command.side_effect = ConnectionError("Mongo is down")
        database.__getitem__.return_value.find_one.side_effect = ConnectionError("Mongo is down")
        app = build(Settings(), database, self.config)
        with (
            TestClient(app) as client,
            patch.object(app.state.ops, "inspect", side_effect=ConnectionError("Docker is down")),
            patch.object(app.state.ops, "http_check", side_effect=ConnectionError("Site is down")),
            patch("pauk.ops.status.GraphDatabase.driver", side_effect=ConnectionError("Neo4j is down")),
            patch("pauk.ops.status.requests.get", side_effect=ConnectionError("Network is down")),
        ):
            response = client.get("/api/ops/state")
            self.assertEqual(response.status_code, 200)
            state = response.json()
            mongo = next(check for check in state["checks"] if check["label"].startswith("MongoDB"))
            self.assertFalse(mongo["healthy"])
            self.assertNotIn("user", state)
            self.assertNotIn("csrf", state)
            self.assertEqual(client.get("/api/ops/operations").status_code, 200)
            self.assertEqual(client.put(
                "/api/ops/schedule", json={"enabled": False, "time": "03:00", "keep": 3}
            ).status_code, 200)
            self.assertFalse(client.cookies)

    def test_log_api_redacts_credentials(self):
        row = self.queued()
        (self.config.state / f"{row['id']}.log").write_text("Bearer secret-token-value")
        response = self.client.get("/api/ops/operations")
        self.assertNotIn("secret-token-value", response.text)
        self.assertNotIn('"args"', response.text)
