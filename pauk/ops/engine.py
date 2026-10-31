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
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import requests

from pauk.cache.graph_snapshot import latest_snapshot
from pauk.jobs.locks import held
from pauk.ops.config import Config
from pauk.ops.store import Store, now

logger = logging.getLogger(__name__)

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


class Engine:
    """Serialize operations and keep a per-operation file outside the databases."""

    def __init__(self, config: Config, store: Store, db):
        self.config, self.store, self.db = config, store, db
        self.stop = threading.Event()
        self.operation = None
        self.thread = None
        self.staging: set[Path] = set()

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
        """Keep stopped services stopped and always restart services we stopped."""
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
                yield
            finally:
                self.stage("Запуск сервисов после обслуживания")
                self.command(self.compose("start", *restart), timeout=180)
                self.wait_databases()

    def backup(self, name: str) -> str:
        backup_id = datetime.now(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:6]
        path = directory(self.config.dumps, backup_id)
        path.mkdir(parents=True, mode=0o700)
        path.chmod(0o700)
        with self.maintenance():
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
        self.stage("Страховочный дамп перед восстановлением")
        safety = self.backup("Перед восстановлением " + backup_id)
        self.stage("Страховочная копия: " + safety)
        path, manifest = self.manifest(backup_id)
        with self.maintenance():
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
        self.wait_databases()
        manifest["restored"] = True
        write_json(path / "manifest.json", manifest)

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
                    result.append(json.loads((path / "release.json").read_text()))
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
        if not destination.exists():
            shutil.copytree(source / "site", destination)
        for file in destination.rglob("*"):
            if file.is_symlink():
                raise ValueError("Символические ссылки в сборке запрещены")
            file.chmod(0o755 if file.is_dir() else 0o644)
        destination.chmod(0o755)
        temporary = self.config.site / f".slot-{uuid4().hex}"
        temporary.symlink_to(Path("releases") / release_id)
        temporary.replace(target)

    def prepare_data(self, name: str, fresh=True):
        self.stage("Подготовка данных карты")
        if self.busy():
            raise RuntimeError("Дождитесь завершения задач воркера")
        snapshots = self.config.essdata / "snapshots" / self.config.stand
        generated = self.config.home / "gui" / self.config.stand / f"ops-{identifier(name)}"
        self.staging.add(generated)
        if fresh:
            snapshot = snapshots / f"graph_snapshot_ops-{name}.json"
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
        previous = self.store.get("preview")
        try:
            self.mount_slot("preview", release_id)
            self.http_check(8521)
        except Exception:
            self.mount_slot("preview", previous)
            raise
        self.store.set("preview", release_id)

    def http_check(self, port: int):
        response = requests.get(f"http://site:{port}/graph-data.json", timeout=15)
        response.raise_for_status()
        response.json()

    def publish(self, release_id: str):
        self.release(release_id)
        previous = self.store.get("live")
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
            self.store.set("live", imported)
            previous = imported
            current.rename(self.config.site / (".previous-main-" + imported))
        self.stage("Переключение основного сайта")
        try:
            self.mount_slot("main", release_id)
            self.http_check(8520)
        except Exception:
            if previous:
                self.mount_slot("main", previous)
            raise
        self.store.set("live", release_id)
        if self.store.get("preview") == release_id:
            self.mount_slot("preview", None)
            self.store.set("preview", None)

    def execute(self, action: str, args: dict):
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
            self.mount_slot("preview", None)
            self.store.set("preview", None)
        elif action == "start-archive":
            port = int(args.get("port", 0))
            if port not in range(8522, 8530):
                raise ValueError("Недопустимый порт")
            if self.store.get(f"archive-{port}"):
                raise ValueError("Порт уже занят")
            try:
                self.mount_slot(f"archive-{port}", item)
                self.http_check(port)
            except Exception:
                self.mount_slot(f"archive-{port}", None)
                raise
            self.store.set(f"archive-{port}", item)
        elif action == "stop-archive":
            for port in range(8522, 8530):
                if self.store.get(f"archive-{port}") == item:
                    self.mount_slot(f"archive-{port}", None)
                    self.store.set(f"archive-{port}", None)
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
