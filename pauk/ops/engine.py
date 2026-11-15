"""Fixed Docker commands and durable operations; never run browser-provided shell."""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager, nullcontext
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import requests

from pauk.cache.graph_snapshot import latest_snapshot
from pauk.jobs.locks import LEASE_MINUTES, held, renew
from pauk.jobs.models import GRAPH
from pauk.ops.config import Config
from pauk.ops.site_files import prepare_site_copy
from pauk.ops.store import Store, now

logger = logging.getLogger(__name__)
GRAPH_RENEW_SECONDS = LEASE_MINUTES * 60 / 3

FILES = ("mongo.archive.gz", "neo4j.dump", "system.dump")
ACTIONS = {
    "create-backup": ("Создание дампа", "Дампы"),
    "verify-backup": ("Проверка дампа", "Дампы"),
    "restore-backup": ("Восстановление баз", "Дампы"),
    "delete-backup": ("Удаление дампа", "Дампы"),
    "build-site": ("Сборка превью", "Сборка"),
    "publish": ("Публикация сайта", "Сайт"),
    "rollback": ("Возврат релиза", "Сайт"),
    "discard-preview": ("Удаление превью", "Сайт"),
    "start-archive": ("Запуск архива", "Сайт"),
    "stop-archive": ("Остановка архива", "Сайт"),
    "publish-pages": ("Обновление GitHub Pages", "Сайт"),
}


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def identifier(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,119}", value):
        raise ValueError("Недопустимый идентификатор")
    return value


def checksum(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def compress_assets(root: Path) -> None:
    """Prepare nginx gzip_static variants without changing source assets."""
    for path in root.rglob("*"):
        if path.is_file() and path.suffix in (".json", ".html", ".js", ".css"):
            with (
                path.open("rb") as source,
                gzip.open(path.with_name(path.name + ".gz"), "wb", compresslevel=6) as output,
            ):
                shutil.copyfileobj(source, output)


def directory(root: Path, name: str) -> Path:
    result = root / identifier(name)
    if result.is_symlink() or result.resolve().parent != root.resolve():
        raise ValueError("Недопустимый путь")
    return result


@contextmanager
def export_graph_lock(db):
    """Keep the graph lease alive while a snapshot export runs."""
    owner = f"ops-export-{uuid4().hex}"
    stopped = threading.Event()
    lost = threading.Event()

    def heartbeat():
        while not stopped.wait(GRAPH_RENEW_SECONDS):
            try:
                if not renew(db, GRAPH, owner):
                    lost.set()
                    return
            except Exception:
                logger.exception("could not renew graph export lock")
                lost.set()
                return

    with held(db, GRAPH, owner=owner):
        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stopped.set()
            thread.join()
        if lost.is_set():
            raise RuntimeError("Graph export lock renewal failed")


class Engine:
    """Serialize operations and keep a per-operation file outside the databases."""

    def __init__(self, config: Config, store: Store, db):
        self.config, self.store, self.db = config, store, db
        self.stop = threading.Event()
        self.operation = None
        self.thread = None
        self.staging: set[Path] = set()
        self.site_state_error: str | None = None

    def compose(self, *args: str) -> list[str]:
        return [
            "docker",
            "compose",
            "--project-name",
            self.config.project,
            "--env-file",
            str(self.config.env_file),
            "-f",
            str(self.config.code / "deploy/compose.yml"),
            *args,
        ]

    def command(
        self, args: list[str], *, timeout=3600, binary: Path | None = None, backup_auth=False, extra_env=None
    ) -> str:
        """Capture output locally; secrets in environment never appear in command arguments."""
        log = self.store.root / f"{self.operation['id'] if self.operation else 'probe'}.log"
        environment = os.environ.copy()
        if backup_auth:
            environment["MONGO_URI"] = os.environ["MONGO_BACKUP_URI"]
        environment.update(extra_env or {})
        with log.open("ab") as output:
            log.chmod(0o600)
            if binary is not None:
                with binary.open("wb") as archive:
                    result = subprocess.run(
                        args, stdout=archive, stderr=output, timeout=timeout, check=False, env=environment
                    )
                text = ""
            else:
                result = subprocess.run(
                    args, stdout=output, stderr=subprocess.STDOUT, timeout=timeout, check=False, env=environment
                )
                text = ""
        if result.returncode:
            raise RuntimeError(f"Команда завершилась с кодом {result.returncode}; подробности в логе")
        return text

    def inspect(self, *args: str):
        result = subprocess.run(self.compose(*args), capture_output=True, text=True, timeout=20, check=True)
        text = result.stdout.strip()
        if not text:
            return []
        try:
            data = json.loads(text)
            return data if isinstance(data, list) else [data]
        except json.JSONDecodeError:
            return [json.loads(line) for line in text.splitlines()]

    def stage(self, title: str) -> None:
        if self.operation:
            self.operation["stages"].append(title)
            self.operation["step"] = len(self.operation["stages"]) - 1
            self.operation["logs"].append(f"{now()} {title}")
            self.store.save(self.operation)

    def busy(self) -> bool:
        return self.db["jobs"].count_documents({"state": {"$in": ["running", "claimed"]}}) > 0

    @contextmanager
    def maintenance(self):
        """Resume previous services unless database recovery is still pending."""
        if self.busy():
            raise RuntimeError("Воркер занят. Дождитесь завершения активных задач.")
        rows = self.inspect("ps", "--all", "--format", "json")
        running = {row["Service"] for row in rows if row.get("State") == "running"}
        if not {"mongo", "neo4j"}.issubset(running):
            raise RuntimeError("Обе базы должны работать до начала обслуживания")
        restart = [service for service in ("neo4j", "admin", "worker") if service in running]
        with held(self.db, "graph", owner="ops"):
            try:
                self.stage("Остановка записи и Neo4j")
                self.command(self.compose("stop", "-t", "300", "worker", "admin"), timeout=330)
                if self.busy():
                    raise RuntimeError("Осталась незавершённая задача; обслуживание отменено")
                self.command(self.compose("stop", "neo4j"), timeout=90)
                yield restart
            finally:
                if not self.store.get("database-recovery"):
                    self.stage("Запуск сервисов после обслуживания")
                    self.command(self.compose("start", *restart), timeout=180)
                    self.wait_databases()

    def backup(self, name: str, *, stopped=False) -> str:
        backup_id = datetime.now(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:6]
        path = directory(self.config.dumps, backup_id)
        path.mkdir(parents=True, mode=0o700)
        path.chmod(0o700)
        with nullcontext() if stopped else self.maintenance():
            self.stage("Дамп MongoDB")
            script = 'exec mongodump --uri="$MONGO_URI" --archive --gzip'
            self.command(
                self.compose("exec", "-T", "-e", "MONGO_URI", "mongo", "sh", "-c", script),
                binary=path / FILES[0],
                backup_auth=True,
            )
            self.stage("Дампы Neo4j и system")
            for database in ("neo4j", "system"):
                self.command(
                    self.compose(
                        "run",
                        "--rm",
                        "--no-deps",
                        "--user",
                        "0",
                        "-v",
                        f"{path}:/backups",
                        "--entrypoint",
                        "neo4j-admin",
                        "neo4j",
                        "database",
                        "dump",
                        database,
                        "--to-path=/backups",
                    )
                )
        self.stage("Контрольные суммы")
        manifest = dict(
            id=backup_id,
            name=name,
            created=now(),
            verified=False,
            restored=False,
            files={file: checksum(path / file) for file in FILES},
            credentials=self.credentials(),
        )
        write_json(path / "manifest.json", manifest)
        self.verify(backup_id)
        return backup_id

    def manifest(self, backup_id: str) -> tuple[Path, dict]:
        path = directory(self.config.dumps, backup_id)
        manifest_path = path / "manifest.json"
        if manifest_path.is_symlink():
            raise ValueError("Недопустимый manifest")
        manifest = json.loads(manifest_path.read_text())
        if set(manifest.get("files", {})) != set(FILES) or manifest.get("id") != backup_id:
            raise ValueError("Неполный состав копии")
        return path, manifest

    def verify(self, backup_id: str) -> None:
        path, manifest = self.manifest(backup_id)
        for file in FILES:
            asset = path / file
            if asset.is_symlink() or not asset.is_file() or checksum(asset) != manifest["files"][file]:
                raise ValueError(f"Не совпала контрольная сумма {file}")
        with gzip.open(path / FILES[0], "rb") as archive:
            while archive.read(1024 * 1024):
                pass
        for database in ("neo4j", "system"):
            self.command(
                self.compose(
                    "run",
                    "--rm",
                    "--no-deps",
                    "--user",
                    "0",
                    "-v",
                    f"{path}:/backups:ro",
                    "--entrypoint",
                    "neo4j-admin",
                    "neo4j",
                    "database",
                    "load",
                    database,
                    "--from-path=/backups",
                    "--info",
                )
            )
        manifest["verified"] = True
        write_json(path / "manifest.json", manifest)

    def restore(self, backup_id: str) -> None:
        self.stage("Проверка выбранного дампа")
        self.verify(backup_id)
        _, selected = self.manifest(backup_id)
        if selected.get("credentials") != self.credentials():
            raise ValueError("Учётные данные баз изменились после создания копии")
        recovery = self.store.get("database-recovery")
        if recovery and backup_id != recovery["safety"]:
            raise ValueError("Сначала восстановите страховочную копию: " + recovery["safety"])
        path, manifest = self.manifest(backup_id)
        with nullcontext(recovery["restart"]) if recovery else self.maintenance() as restart:
            if recovery:
                self.command(self.compose("stop", "-t", "300", "worker", "admin"), timeout=330)
                self.command(self.compose("stop", "neo4j"), timeout=90)
                self.command(self.compose("start", "mongo"), timeout=180)
                self.wait_mongo()
            else:
                self.stage("Страховочный дамп перед восстановлением")
                safety = self.backup("Перед восстановлением " + backup_id, stopped=True)
                recovery = dict(target=backup_id, safety=safety, restart=restart)
                self.stage("Страховочная копия: " + safety)
            recovery["ready"] = False
            self.store.set("database-recovery", recovery)
            self.stage("Восстановление MongoDB")
            database = self.db.name
            if not re.fullmatch(r"[a-zA-Z0-9_-]+", database) or database in ("admin", "local", "config"):
                raise ValueError("Недопустимая база PAUK")
            drop = f"db.getSiblingDB({json.dumps(database)}).dropDatabase()"
            self.command(
                self.compose(
                    "exec",
                    "-T",
                    "-e",
                    "MONGO_URI",
                    "mongo",
                    "sh",
                    "-c",
                    'exec mongo "$MONGO_URI" --quiet --eval ' + shlex.quote(drop),
                ),
                backup_auth=True,
            )
            script = 'exec mongorestore --uri="$MONGO_URI" --archive=/backups/mongo.archive.gz --gzip --drop'
            self.command(
                self.compose(
                    "run",
                    "--rm",
                    "--no-deps",
                    "--user",
                    "0",
                    "-e",
                    "MONGO_URI",
                    "-v",
                    f"{path}:/backups:ro",
                    "mongo",
                    "sh",
                    "-c",
                    script,
                ),
                backup_auth=True,
            )
            self.stage("Восстановление Neo4j и system")
            for database in ("neo4j", "system"):
                script = (
                    f"neo4j-admin database load {database} --from-path=/backups "
                    "--overwrite-destination=true && chown -R neo4j:neo4j /data"
                )
                self.command(
                    self.compose(
                        "run",
                        "--rm",
                        "--no-deps",
                        "--user",
                        "0",
                        "-v",
                        f"{path}:/backups:ro",
                        "--entrypoint",
                        "sh",
                        "neo4j",
                        "-c",
                        script,
                    )
                )
            self.stage("Проверка запуска баз")
            self.command(self.compose("start", "mongo", "neo4j"), timeout=180)
            self.wait_databases()
            manifest["restored"] = True
            write_json(path / "manifest.json", manifest)
            recovery["ready"] = True
            self.store.set("database-recovery", recovery)
        self.finish_recovery()

    def finish_recovery(self):
        """Resume writers only after a durable record of successful database loads."""
        recovery = self.store.get("database-recovery")
        if not recovery or not recovery.get("ready"):
            return
        self.wait_databases()
        if recovery["restart"]:
            self.command(self.compose("start", *recovery["restart"]), timeout=180)
        self.store.set("database-recovery", None)

    def wait_mongo(self):
        """Wait for Mongo independently of the offline graph during recovery."""
        for attempt in range(60):
            try:
                self.db.client.admin.command("ping")
                return
            except Exception:
                if attempt == 59:
                    raise
                time.sleep(2)

    def credentials(self) -> str:
        value = "\0".join(
            (
                os.environ.get("MONGO_BACKUP_URI", ""),
                os.environ.get("MONGO_URI", ""),
                os.environ.get("NEO4J_PASSWORD", ""),
                self.db.name,
            )
        )
        return hashlib.sha256(value.encode()).hexdigest()

    def wait_databases(self):
        for _ in range(60):
            try:
                self.db.client.admin.command("ping")
                self.command(
                    self.compose(
                        "exec", "-T", "neo4j", "sh", "-c", 'cypher-shell -u neo4j -p "${NEO4J_AUTH#*/}" "RETURN 1"'
                    ),
                    timeout=10,
                )
                return
            except Exception:
                time.sleep(2)
        raise RuntimeError("Базы не прошли проверку после восстановления")

    def backups(self) -> list[dict]:
        """List managed copies and older database archives without modifying them."""
        result = []
        self.config.dumps.mkdir(parents=True, exist_ok=True)
        for path in self.config.dumps.iterdir():
            if not path.is_dir() or path.is_symlink():
                continue
            try:
                _, manifest = self.manifest(path.name)
                result.append(
                    {
                        **{key: manifest[key] for key in ("id", "name", "created", "verified", "restored")},
                        "size": size(sum((path / file).stat().st_size for file in FILES)),
                        "managed": True,
                        "components": ["MongoDB", "Neo4j", "system", "manifest"],
                    }
                )
                continue
            except (OSError, ValueError, KeyError):
                pass
            try:
                assets = [
                    file for file in path.rglob("*")
                    if file.is_file() and not file.is_symlink()
                    and file.name.endswith((".dump", ".archive.gz", ".jsonl", ".bson", ".bson.gz", ".db", ".sqlite", ".sqlite3", ".data.tar.gz"))
                ]
                if not assets:
                    continue
                components = set()
                for file in assets:
                    if file.name == "system.dump":
                        components.add("system")
                    elif file.name.endswith(".dump"):
                        components.add("Neo4j")
                    elif file.name.endswith((".db", ".sqlite", ".sqlite3")):
                        components.add("SQLite")
                    elif file.name.endswith(".jsonl"):
                        components.add("экспорт MongoDB")
                    elif file.name.endswith(".data.tar.gz"):
                        components.add("архив данных")
                    else:
                        components.add("MongoDB")
                result.append(dict(
                    id=path.name, name=path.name,
                    created=datetime.fromtimestamp(max(file.stat().st_mtime for file in assets), ZoneInfo("UTC")).isoformat(),
                    size=size(sum(file.stat().st_size for file in assets)),
                    verified=False, restored=False, managed=False, components=sorted(components),
                ))
            except OSError:
                continue
        return sorted(result, key=lambda row: row["created"], reverse=True)

    def releases(self) -> list[dict]:
        self.config.releases.mkdir(parents=True, exist_ok=True)
        result = []
        for path in self.config.releases.iterdir():
            try:
                if not path.is_symlink():
                    metadata = json.loads((path / "release.json").read_text())
                    if isinstance(metadata, dict) and isinstance(metadata.get("id"), str):
                        result.append(metadata)
            except (OSError, ValueError):
                pass
        return result

    def release(self, release_id: str) -> tuple[Path, dict]:
        path = directory(self.config.releases, release_id)
        return path, json.loads((path / "release.json").read_text())

    def mount_slot(self, slot: str, release_id: str | None):
        """Replace a relative symlink atomically within nginx's mounted directory."""
        self.config.site.mkdir(parents=True, exist_ok=True)
        target = self.config.site / slot
        if target.exists() and not target.is_symlink():
            if target.is_dir() and not any(target.iterdir()):
                target.rmdir()
            else:
                raise RuntimeError(f"{slot} — обычный каталог. Нужен первоначальный импорт релиза.")
        if release_id is None:
            target.unlink(missing_ok=True)
            return
        source, _ = self.release(release_id)
        destination = self.config.site / "releases" / release_id
        destination.parent.mkdir(parents=True, exist_ok=True)
        prepare_site_copy(source / "site", destination)
        temporary = self.config.site / f".slot-{uuid4().hex}"
        temporary.symlink_to(Path("releases") / release_id)
        temporary.replace(target)

    def reconcile_site(self):
        """Record the actual slots without publishing or consulting MongoDB."""
        pending = self.store.get("slot-switch")
        if pending:
            try:
                self.mount_slot(pending["slot"], pending["previous"])
                if pending["port"] is not None and pending["previous"] is not None:
                    self.http_check(pending["port"])
                self.store.set_many({pending["key"]: pending["previous"], "slot-switch": None})
            except Exception as error:
                self.site_state_error = f"Не удалось восстановить слот {pending['slot']}: {error}"
                return
        values = {}
        slots = [("main", "live"), ("preview", "preview")]
        slots.extend((f"archive-{port}", f"archive-{port}") for port in range(8522, 8530))
        errors = []
        for slot, key in slots:
            try:
                path = self.config.site / slot
                if slot == "main" and not path.exists() and not path.is_symlink():
                    previous = self.store.get("live")
                    if isinstance(previous, str) and previous.startswith("imported-"):
                        backup = self.config.site / f".previous-main-{identifier(previous)}"
                        if backup.is_dir() and not backup.is_symlink():
                            backup.rename(path)
                if path.is_symlink():
                    target = Path(os.readlink(path))
                    if len(target.parts) != 2 or target.parts[0] != "releases":
                        raise RuntimeError(f"Недопустимая ссылка слота {slot}: {target}")
                    release_id = identifier(target.parts[1])
                    destination = self.config.site / target
                    if destination.is_symlink() or not destination.is_dir():
                        raise RuntimeError(f"Поврежденная ссылка слота {slot}: {target}")
                    _, metadata = self.release(release_id)
                    if not isinstance(metadata, dict) or metadata.get("id") != release_id:
                        raise RuntimeError(f"Некорректные метаданные релиза {release_id}")
                    values[key] = release_id
                elif not path.exists() or (path.is_dir() and not any(path.iterdir())) or slot == "main" and path.is_dir():
                    values[key] = None
                else:
                    raise RuntimeError(f"Неуправляемый слот {slot}: {path}")
            except (OSError, ValueError, RuntimeError, KeyError) as error:
                values[key] = None
                errors.append(f"{slot}: {error}")
        try:
            self.store.set_many(values)
        except Exception as error:
            errors.append(f"Не удалось сохранить состояние сайта: {error}")
        self.site_state_error = "; ".join(errors) or None

    def switch_slot(self, slot: str, key: str, release_id: str | None, port: int | None = None):
        """Restore the old slot if either verification or recording fails."""
        if self.site_state_error:
            raise RuntimeError(self.site_state_error)
        if self.store.get("slot-switch"):
            raise RuntimeError("Сначала завершите восстановление предыдущего переключения слота")
        previous = self.store.get(key)
        self.store.set("slot-switch", dict(
            slot=slot, key=key, previous=previous, target=release_id, port=port,
            operation=self.operation["id"] if self.operation else None,
        ))
        try:
            self.mount_slot(slot, release_id)
            if port is not None:
                self.http_check(port)
            self.store.set_many({key: release_id, "slot-switch": None})
        except Exception:
            try:
                self.mount_slot(slot, previous)
                if port is not None and previous is not None:
                    self.http_check(port)
                self.store.set_many({key: previous, "slot-switch": None})
            except Exception as error:
                self.site_state_error = f"Не удалось восстановить слот {slot}: {error}"
                raise RuntimeError(self.site_state_error) from error
            raise

    def prepare_data(self, name: str, fresh=True):
        self.stage("Подготовка данных карты")
        if self.busy():
            raise RuntimeError("Дождитесь завершения задач воркера")
        snapshots = self.config.essdata / "snapshots" / self.config.stand
        generated = self.config.home / "gui" / self.config.stand / f"ops-{identifier(name)}"
        self.staging.add(generated)
        if fresh:
            snapshot = snapshots / f"graph_snapshot_ops-{name}.json"
            with export_graph_lock(self.db):
                self.command(
                    self.compose(
                        "exec", "-T", "worker", "pauk", "cache", "export", "--output", f"/app/data/cache/{snapshot.name}"
                    )
                )
        else:
            previous = self.store.get("last-snapshot")
            saved = directory(snapshots, previous) if previous else None
            snapshot = saved if saved and saved.is_file() else latest_snapshot(snapshots)
        self.store.set("last-snapshot", snapshot.name)
        self.command(
            self.compose(
                "run",
                "--rm",
                "--no-deps",
                "worker",
                "pauk",
                "gui",
                "build",
                "--cache",
                f"/app/data/cache/{snapshot.name}",
                "--out-dir",
                f"/app/data/gui/ops-{name}",
            )
        )
        return generated, snapshot.name

    def build_site(self, fresh=True):
        release_id = datetime.now(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:6]
        data, snapshot = self.prepare_data(release_id, fresh)
        path = directory(self.config.releases, release_id)
        path.mkdir(parents=True)
        shutil.copytree(data / "public", path / "public")
        self.stage("Сборка сайта из установленной версии кода")
        self.command(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{self.config.code}:/source:ro",
                "-v",
                f"{data}:/data:ro",
                "-v",
                f"{path}:/output",
                "node:24-bookworm-slim",
                "sh",
                "-c",
                "mkdir /tmp/web && tar -C /source/pauk/gui/web --exclude=node_modules --exclude=dist --exclude=dist-public -cf - . "
                "| tar -C /tmp/web -xf - "
                "&& cd /tmp/web && npm ci && node --input-type=module -e '"
                'import {build} from "vite"; await build({publicDir:"/data/private",'
                'build:{outDir:"/output/site",rolldownOptions:{input:"index.html"}}});' + "'",
            ]
        )
        required = ("index.html", "graph-data.json", "authors-detail.json", "pubs-detail.json", "repos-detail.json")
        if not all((path / "site" / name).is_file() for name in required):
            raise ValueError("В сборке отсутствуют обязательные файлы")
        self.stage("Сжатие статических файлов")
        compress_assets(path / "site")
        result = subprocess.run(
            ["git", "-c", f"safe.directory={self.config.code}", "-C", str(self.config.code), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        metadata = dict(
            id=release_id,
            commit=result.stdout.strip()[:12],
            branch="Установленная версия",
            created=now(),
            snapshot=snapshot,
        )
        write_json(path / "release.json", metadata)
        self.stage("Публикация превью и HTTP-проверка")
        self.switch_slot("preview", "preview", release_id, 8521)

    def http_check(self, port: int, full: bool = True):
        names = ("graph-data", "authors-detail", "pubs-detail", "repos-detail") if full else ("graph-data",)
        for name in names:
            response = requests.get(f"http://site:{port}/{name}.json", timeout=15)
            response.raise_for_status()
            response.json()

    def publish(self, release_id: str):
        self.release(release_id)
        current = self.config.site / "main"
        if current.is_dir() and not current.is_symlink():
            imported = "imported-" + uuid4().hex[:8]
            path = directory(self.config.releases, imported)
            path.mkdir(parents=True)
            shutil.copytree(current, path / "site")
            write_json(
                path / "release.json",
                dict(
                    id=imported,
                    commit="—",
                    branch="Установленный сайт",
                    created=now(),
                    snapshot="Импортирован с сервера",
                ),
            )
            try:
                self.store.set("live", imported)
            except Exception:
                self.reconcile_site()
                raise
            try:
                current.rename(self.config.site / (".previous-main-" + imported))
            except Exception:
                self.reconcile_site()
                raise
        self.stage("Переключение основного сайта")
        self.switch_slot("main", "live", release_id, 8520)
        if self.store.get("preview") == release_id:
            self.switch_slot("preview", "preview", None)

    def execute(self, action: str, args: dict):
        recovery = self.store.get("database-recovery")
        if recovery and action not in ("restore-backup", "verify-backup"):
            raise RuntimeError("Требуется восстановить страховочную копию: " + recovery["safety"])
        if self.site_state_error and action in (
            "build-site", "publish", "rollback", "discard-preview", "start-archive", "stop-archive", "publish-pages"
        ):
            raise RuntimeError(self.site_state_error)
        item = args.get("id", "")
        if action == "create-backup":
            self.backup(args.get("name", "Резервная копия"))
            if args.get("automatic"):
                keep = self.store.get("schedule", {"keep": 3})["keep"]
                automatic = [copy for copy in self.backups() if copy["name"] == "Автоматическая копия"]
                for copy in automatic[keep:]:
                    path, _ = self.manifest(copy["id"])
                    shutil.rmtree(path)
        elif action == "verify-backup":
            self.stage("Проверка целостности")
            self.verify(item)
        elif action == "restore-backup":
            self.restore(item)
        elif action == "delete-backup":
            if sum(copy["managed"] for copy in self.backups()) <= 1:
                raise ValueError("Последнюю копию удалять нельзя")
            path, _ = self.manifest(item)
            self.stage("Удаление выбранной копии")
            shutil.rmtree(path)
        elif action == "build-site":
            self.build_site(args.get("fresh", True))
        elif action in ("publish", "rollback"):
            self.publish(item)
        elif action == "discard-preview":
            self.switch_slot("preview", "preview", None)
        elif action == "start-archive":
            port = int(args.get("port", 0))
            if port not in range(8522, 8530):
                raise ValueError("Недопустимый порт")
            if self.store.get(f"archive-{port}"):
                raise ValueError("Порт уже занят")
            self.switch_slot(f"archive-{port}", f"archive-{port}", item, port)
        elif action == "stop-archive":
            for port in range(8522, 8530):
                if self.store.get(f"archive-{port}") == item:
                    self.switch_slot(f"archive-{port}", f"archive-{port}", None)
        elif action == "publish-pages":
            script = self.config.pages_script
            if not script or not script.is_file():
                raise RuntimeError("Скрипт GitHub Pages не настроен")
            selected = item or self.store.get("live")
            public = None
            if selected:
                release, _ = self.release(selected)
                if (release / "public").is_dir():
                    public = release / "public"
            if public is None:
                data, _ = self.prepare_data(uuid4().hex)
                public = data / "public"
            self.stage("Публичные данные, загрузка и ожидание GitHub Pages")
            result = self.store.root / f"pages-{uuid4().hex}.json"
            self.command(
                ["bash", str(script)],
                extra_env={
                    "PAUK_PUBLIC_DIR": str(public),
                    "PAUK_REPORTS_DIR": str(self.config.essdata / "reports" / self.config.stand),
                    "PAUK_PAGES_RESULT": str(result),
                },
            )
            metadata = json.loads(result.read_text())
            if metadata.get("conclusion") != "success":
                raise RuntimeError("GitHub Pages workflow не завершился успешно")
            response = requests.get("https://itmo-nss-team.github.io/PAUK/", timeout=30)
            response.raise_for_status()
            self.store.set(
                "publicRelease", dict(source=metadata["headBranch"], commit=metadata["headSha"][:12], created=now())
            )
        else:
            raise ValueError("Неизвестная операция")

    def run_once(self) -> bool:
        operation = next((row for row in reversed(self.store.operations()) if row["state"] == "queued"), None)
        if not operation:
            return False
        self.operation = operation
        started = time.monotonic()
        operation["state"] = "running"
        self.store.save(operation)
        try:
            self.execute(operation["action"], operation["args"])
            operation["state"] = "done"
            operation["step"] = len(operation["stages"])
        except Exception as error:
            operation["state"] = "failed"
            operation["logs"].append(f"{now()} {error}")
        finally:
            for path in self.staging:
                if not path.is_symlink() and path.parent == self.config.home / "gui" / self.config.stand:
                    shutil.rmtree(path, ignore_errors=True)
            self.staging.clear()
            operation["duration"] = f"{int(time.monotonic() - started)} с"
            self.store.save(operation)
            self.operation = None
        return True

    def schedule(self):
        if self.store.get("database-recovery"):
            return
        config = self.store.get("schedule", {"enabled": False, "time": "03:00", "keep": 3})
        current = datetime.now(ZoneInfo("Europe/Moscow"))
        day = current.date().isoformat()
        if (
            config["enabled"]
            and current.strftime("%H:%M") >= config["time"]
            and self.store.get("scheduled-day") != day
            and not self.busy()
        ):
            title, kind = ACTIONS["create-backup"]
            self.store.enqueue(
                dict(
                    action="create-backup",
                    title=title,
                    kind=kind,
                    args={"name": "Автоматическая копия", "automatic": True},
                ),
                "schedule",
            )
            self.store.set("scheduled-day", day)

    def start(self):
        if self.store.get("database-recovery"):
            try:
                self.command(self.compose("stop", "-t", "300", "worker", "admin"), timeout=330)
                self.finish_recovery()
            except Exception:
                logger.exception("Database recovery remains pending; could not reconcile services")
        self.reconcile_site()
        self.store.recover()

        def run():
            while not self.stop.is_set():
                try:
                    if not self.run_once():
                        self.schedule()
                except Exception:
                    logger.exception("OPS runner iteration failed")
                self.stop.wait(2)

        self.thread = threading.Thread(target=run, name="pauk-ops", daemon=True)
        self.thread.start()


def size(value: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024 or unit == "ТБ":
            return f"{value:.1f} {unit}"
        value /= 1024
    return "—"
