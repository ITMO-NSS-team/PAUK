"""Durable, single-writer operation queue and settings."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    """Keep control-plane state outside MongoDB and Neo4j backups."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = root / "ops.sqlite3"
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS settings (id TEXT PRIMARY KEY, body TEXT NOT NULL)")

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def get(self, key: str, default=None):
        with self.connect() as db:
            row = db.execute("SELECT body FROM settings WHERE id=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value) -> None:
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))

    def operations(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT body FROM operations ORDER BY rowid DESC LIMIT 200").fetchall()
        return [json.loads(row[0]) for row in rows]

    def save(self, operation: dict) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO operations VALUES (?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                (operation["id"], json.dumps(operation, ensure_ascii=False)),
            )

    def enqueue(self, command: dict, actor: str) -> dict:
        operation = dict(
            id=uuid4().hex[:12],
            title=command["title"],
            kind=command["kind"],
            action=command["action"],
            args=command.get("args", {}),
            actor=actor,
            state="queued",
            started=now(),
            duration="—",
            stages=[],
            step=0,
            logs=[],
        )
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute("SELECT body FROM operations").fetchall()
            if any(json.loads(row[0])["state"] in ("queued", "running") for row in active):
                raise ValueError("Дождитесь завершения текущей операции")
            db.execute("INSERT INTO operations VALUES (?,?)", (operation["id"], json.dumps(operation)))
        return operation

    def recover(self) -> None:
        """Never replay interrupted destructive operations automatically."""
        for operation in self.operations():
            if operation["state"] == "running":
                operation["state"] = "failed"
                operation["logs"].append("OPS перезапущен. Проверьте состояние перед новым запуском.")
                self.save(operation)
