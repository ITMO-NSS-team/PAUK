"""OPS API and frontend, separate from the public graph map."""

from __future__ import annotations

import fcntl
import os
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pauk.ops.config import Config
from pauk.ops.engine import ACTIONS, Engine, identifier
from pauk.ops.status import observed
from pauk.ops.store import Store
from pauk.redaction import configured_secret_values, redact_text
from pauk.settings import Settings
from pauk.storage import get_mongo_client


class Command(BaseModel):
    action: Literal[
        "create-backup",
        "verify-backup",
        "restore-backup",
        "delete-backup",
        "build-site",
        "publish",
        "rollback",
        "discard-preview",
        "start-archive",
        "stop-archive",
        "publish-pages",
    ]
    id: str = Field(default="", max_length=120)
    name: str = Field(default="Резервная копия", min_length=1, max_length=120)
    fresh: bool = True
    port: int = Field(default=8522, ge=8522, le=8529)
    confirmation: str = Field(default="", max_length=160)


class Schedule(BaseModel):
    enabled: bool
    time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    keep: int = Field(ge=1, le=100)


def build(config=None, db=None, ops_config: Config | None = None):
    """Build the open control plane without graph-editor accounts or sessions."""
    config = config or Settings()
    client = get_mongo_client(config, timeout_ms=2000) if db is None else None
    database = client[config.mongo_db] if client is not None else db
    settings = ops_config or Config.environment()
    store = Store(settings.state)
    engine = Engine(settings, store, database)

    @asynccontextmanager
    async def lifespan(application):
        try:
            with (settings.state / "runner.lock").open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                engine.start()
                try:
                    yield
                finally:
                    engine.stop.set()
                    if engine.thread:
                        engine.thread.join()
        finally:
            if client is not None:
                client.close()

    app = FastAPI(title="PAUK OPS", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.db = database
    app.state.ops = engine

    @app.get("/ops/health")
    def health():
        return {"ok": True}

    @app.get("/")
    def home():
        return RedirectResponse("/ops/", status_code=307)

    @app.get("/ops")
    def redirect():
        return RedirectResponse("/ops/", status_code=307)

    @app.get("/ops/")
    def page():
        path = settings.assets / "ops.html"
        if not path.is_file():
            raise HTTPException(503, "Интерфейс OPS не собран")
        return FileResponse(path, headers={"Cache-Control": "no-store"})

    @app.get("/api/ops/state")
    def state():
        result = observed(engine)
        result.update(
            stand=settings.stand,
            host="einsteinium.nsslab",
            operations=operations(),
        )
        return result

    def operations():
        result = []
        for row in store.operations():
            log = settings.state / f"{row['id']}.log"
            lines = row["logs"][:]
            if log.is_file():
                with log.open("rb") as stream:
                    stream.seek(max(0, log.stat().st_size - 64000))
                    lines += stream.read().decode("utf-8", errors="replace").splitlines()
            secrets = (
                *configured_secret_values(),
                str(engine.config.env_file),
                os.environ.get("MONGO_URI", ""),
                os.environ.get("MONGO_BACKUP_URI", ""),
            )
            lines = [redact_text(line, secrets) for line in lines]
            result.append(
                {key: value for key, value in {**row, "logs": lines}.items() if key not in ("args", "action")}
            )
        return result

    @app.get("/api/ops/operations")
    def operation_list():
        return operations()

    @app.post("/api/ops/commands", status_code=202)
    def command(body: Command):
        if body.id:
            try:
                identifier(body.id)
            except ValueError as error:
                raise HTTPException(422, str(error)) from None
        if body.action in ("restore-backup", "delete-backup", "publish", "rollback", "publish-pages"):
            expected = f"ВОССТАНОВИТЬ {settings.stand}" if body.action == "restore-backup" else "ПОДТВЕРЖДАЮ"
            if body.confirmation != expected:
                raise HTTPException(422, "Нужно подтверждение операции")
        if body.action == "publish-pages" and not settings.pages_script:
            raise HTTPException(409, "Скрипт GitHub Pages не настроен")
        if any(row["state"] in ("running", "queued") for row in store.operations()):
            raise HTTPException(409, "Дождитесь завершения текущей операции")
        title, kind = ACTIONS[body.action]
        try:
            return store.enqueue(
                dict(
                    action=body.action,
                    title=title,
                    kind=kind,
                    args=body.model_dump(exclude={"action", "confirmation"}),
                ),
                "anonymous",
            )
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    @app.put("/api/ops/schedule")
    def schedule(body: Schedule):
        store.set("schedule", body.model_dump())
        return body

    if (settings.assets / "assets").is_dir():
        app.mount("/ops/assets", StaticFiles(directory=str(settings.assets / "assets")))
    return app
