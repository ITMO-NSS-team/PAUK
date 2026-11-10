"""Opt-in full restore against fresh, isolated Docker databases."""

import os
import secrets
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pymongo import MongoClient

from pauk.ops.config import Config
from pauk.ops.engine import Engine
from pauk.ops.status import observed
from pauk.ops.store import Store


@unittest.skipUnless(
    os.environ.get("PAUK_TEST_OPS_RESTORE") == "1", "Set PAUK_TEST_OPS_RESTORE=1 for isolated Docker restore"
)
class FullRestoreTest(unittest.TestCase):
    def test_both_databases_restore_and_services_restart(self):
        root = Path(tempfile.mkdtemp(prefix="pauk-ops-isolated-", dir="/tmp"))
        home = root / "home"
        essdata = root / "essdata"
        code = home / "code/deploy"
        code.mkdir(parents=True)
        env = root / "env"
        env.write_text("")
        password = "only-local-ops-check"
        config = Config(
            home=home,
            essdata=essdata,
            stand="test",
            env_file=env,
            state=home / "ops",
            assets=root / "web",
            project="pauk-ops-test-" + secrets.token_hex(4),
        )
        compose = code / "compose.yml"
        compose.write_text(f'''services:
          mongo:
            image: mongo:4.4
            environment:
              MONGO_INITDB_ROOT_USERNAME: test
              MONGO_INITDB_ROOT_PASSWORD: {password}
            ports: ["127.0.0.1::27017"]
            volumes: ["{home}/mongo:/data/db"]
          neo4j:
            image: neo4j:2026.05.0
            ports: ["127.0.0.1::7687"]
            environment:
              NEO4J_AUTH: neo4j/{password}
              NEO4J_server_memory_heap_initial__size: 256m
              NEO4J_server_memory_heap_max__size: 256m
              NEO4J_server_memory_pagecache_size: 128m
            volumes: ["{home}/neo4j:/data"]
          admin:
            image: debian:12
            init: true
            command: [sleep, infinity]
          worker:
            image: debian:12
            init: true
            command: [sleep, infinity]
        ''')
        store = Store(config.state)
        base = ["docker", "compose", "--project-name", config.project, "--env-file", str(env), "-f", str(compose)]

        def cmd(*args, **kwargs):
            return subprocess.run([*base, *args], check=True, timeout=180, **kwargs)

        environment = patch.dict(
            os.environ,
            {
                "MONGO_BACKUP_URI": f"mongodb://test:{password}@mongo:27017/?authSource=admin",
                "NEO4J_PASSWORD": password,
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        try:
            cmd("up", "-d")
            port = cmd("port", "mongo", "27017", capture_output=True, text=True).stdout.strip().rsplit(":", 1)[1]
            client = MongoClient(
                f"mongodb://test:{password}@127.0.0.1:{port}/?authSource=admin", serverSelectionTimeoutMS=1000
            )
            for _ in range(60):
                try:
                    client.admin.command("ping")
                    ready = subprocess.run(
                        [*base, "exec", "-T", "neo4j", "cypher-shell", "-u", "neo4j", "-p", password, "RETURN 1"],
                        capture_output=True,
                        timeout=10,
                    )
                    if ready.returncode == 0:
                        break
                except Exception:
                    pass
                time.sleep(2)
            else:
                raise RuntimeError("DB startup timeout")
            db = client["pauk_ops_check"]
            db.examples.insert_one({"_id": "data", "value": "before"})
            engine = Engine(config, store, db)
            bolt_port = cmd("port", "neo4j", "7687", capture_output=True, text=True).stdout.strip().rsplit(":", 1)[1]
            os.environ["NEO4J_URI"] = f"bolt://127.0.0.1:{bolt_port}"
            with patch("pauk.ops.status.requests.get", side_effect=RuntimeError("External probes disabled")):
                state = observed(engine)
            self.assertTrue(
                next(check for check in state["checks"] if check["label"] == "Neo4j · запрос к графу")["healthy"]
            )

            engine.command(
                engine.compose(
                    "exec",
                    "-T",
                    "neo4j",
                    "cypher-shell",
                    "-u",
                    "neo4j",
                    "-p",
                    password,
                    "CREATE (:OpsCheck {value:'before'})",
                )
            )
            backup = engine.backup("Isolated integration check")
            db.examples.update_one({"_id": "data"}, {"$set": {"value": "after"}})
            db.extra.insert_one({"_id": "new-after-backup"})
            engine.command(
                engine.compose(
                    "exec",
                    "-T",
                    "neo4j",
                    "cypher-shell",
                    "-u",
                    "neo4j",
                    "-p",
                    password,
                    "MATCH (n:OpsCheck) SET n.value='after'",
                )
            )
            engine.restore(backup)
            self.assertEqual(db.examples.find_one({"_id": "data"})["value"], "before")
            self.assertEqual(db.extra.count_documents({}), 0)
            result = cmd(
                "exec",
                "-T",
                "neo4j",
                "cypher-shell",
                "-u",
                "neo4j",
                "-p",
                password,
                "MATCH (n:OpsCheck) RETURN n.value",
                capture_output=True,
                text=True,
            )
            self.assertIn("before", result.stdout)
            self.assertNotIn("after", result.stdout)
            self.assertTrue(
                all(row["State"] == "running" for row in engine.inspect("ps", "--all", "--format", "json"))
            )
            self.assertEqual(len(engine.backups()), 2)
            print(
                "PASS: Mongo + Neo4j full backup, safety copy, restore, removal of post-backup collection, service restart"
            )
        finally:
            client.close() if "client" in locals() else None
            try:
                cmd("down", "--remove-orphans")
            finally:
                subprocess.run(
                    ["docker", "run", "--rm", "-v", f"{root}:/cleanup", "debian:12", "sh", "-c", "rm -rf /cleanup/*"],
                    check=True,
                    timeout=60,
                )
                root.rmdir()
