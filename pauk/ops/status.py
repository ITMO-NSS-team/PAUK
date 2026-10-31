"""Observed service health, storage usage and releases; no synthetic green states."""

from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import requests
from neo4j import GraphDatabase, Query

from pauk.ops.engine import Engine, size

SERVICES = {
    "mongo": ("MongoDB", "Исходные данные и очередь задач", "database"),
    "neo4j": ("Neo4j", "Граф публикаций и репозиториев", "nodes"),
    "admin": ("Админка", "Редактирование данных", "window"),
    "worker": ("Воркер", "Сбор и обработка публикаций", "activity"),
    "site": ("Сайт", "Карта научных связей", "globe"),
    "ops": ("OPS", "Управление эксплуатацией", "settings"),
}


class TokenIssue(ValueError):
    """A credential problem distinct from network availability."""


def published_ports(row: dict) -> str:
    """Format host ports reported by Compose, collapsing contiguous ranges."""
    ports = sorted({item["PublishedPort"] for item in row.get("Publishers") or [] if item.get("PublishedPort")})
    ranges = []
    for port in ports:
        if ranges and port == ranges[-1][-1] + 1:
            ranges[-1].append(port)
        else:
            ranges.append([port])
    return ", ".join(str(group[0]) if len(group) == 1 else f"{group[0]}-{group[-1]}" for group in ranges) or "—"


def observed(engine: Engine):
    services, checks, storage = [], [], []
    error = ""
    try:
        rows = engine.inspect("ps", "--all", "--format", "json")
        by_name = {row["Service"]: row for row in rows}
        stats = subprocess.run(
            [
                "docker",
                "stats",
                "--no-stream",
                "--format",
                "{{json .}}",
                *[row["ID"] for row in rows if row.get("State") == "running" and row.get("ID")],
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
        usage = {row["ID"]: row for row in (json.loads(line) for line in stats.stdout.splitlines())}
        for service, (name, role, icon) in SERVICES.items():
            row = by_name.get(service, {})
            stat = next((value for key, value in usage.items() if row.get("ID", "!").startswith(key)), {})
            good = row.get("State") == "running" and row.get("Health", "") in ("", "healthy")
            services.append(
                dict(
                    name=name,
                    role=role,
                    icon=icon,
                    healthy=good,
                    status="Работает" if good else row.get("Health") or row.get("State") or "Недоступен",
                    version=row.get("Image", "—"),
                    ports=published_ports(row),
                    cpu=stat.get("CPUPerc", "—"),
                    memory=stat.get("MemUsage", "—").split(" / ")[0],
                )
            )
    except Exception:
        error = "Не удалось прочитать состояние Docker"
        services = [
            dict(name=name, role=role, icon=icon, healthy=False, status="Нет данных", version="—", ports="—", cpu="—", memory="—")
            for name, role, icon in SERVICES.values()
        ]
    for name, path in (("Локальный диск", engine.config.home), ("essdata · общее", engine.config.essdata)):
        try:
            stat = os.statvfs(path)
            total, used = stat.f_blocks * stat.f_frsize, (stat.f_blocks - stat.f_bfree) * stat.f_frsize
            storage.append(dict(name=name, used=size(used), total=size(total), percent=round(used / total * 100)))
        except OSError:
            storage.append(dict(name=name, used="—", total="—", percent=None))

    def check(label, probe):
        try:
            probe()
            return dict(label=label, healthy=True, detail="")
        except TokenIssue as error:
            return dict(label=label, healthy=False, detail="Токен", warning=str(error))
        except Exception:
            return dict(label=label, healthy=False, detail="Недоступен")

    def graph():
        with GraphDatabase.driver(
            os.environ.get("NEO4J_URI", "bolt://neo4j:7687"),
            auth=("neo4j", os.environ.get("NEO4J_PASSWORD", "")),
            connection_timeout=3,
            max_transaction_retry_time=0,
        ) as driver:
            driver.execute_query(Query("RETURN 1", timeout=3))

    def heartbeat():
        cutoff = datetime.now(UTC) - timedelta(minutes=2)
        if not engine.db["job_workers"].find_one({"seen_at": {"$gte": cutoff}}):
            raise ValueError("Нет heartbeat")

    def github():
        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            raise TokenIssue("GitHub-токен отсутствует.")
        response = requests.get("https://api.github.com/user", headers={"Authorization": f"Bearer {token}"}, timeout=5)
        if response.status_code == 401:
            raise TokenIssue("GitHub-токен требует обновления.")
        response.raise_for_status()

    def openrouter():
        token = os.environ.get("OPENROUTER_API_KEY", "")
        if not token:
            raise TokenIssue("OpenRouter-токен отсутствует.")
        proxy = os.environ.get("OPENROUTER_PROXY_URL", "")
        response = requests.get(
            "https://openrouter.ai/api/v1/auth/key",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
            proxies={"http": proxy, "https": proxy} if proxy else None,
        )
        if response.status_code == 401:
            raise TokenIssue("OpenRouter-токен требует обновления.")
        response.raise_for_status()

    probes = [
        ("MongoDB · подключение", lambda: engine.db.client.admin.command("ping")),
        ("Neo4j · запрос к графу", graph),
        ("Воркер · heartbeat", heartbeat),
        ("Сайт · данные карты", lambda: engine.http_check(8520)),
        ("GitHub API", github),
        ("OpenRouter · соединение", openrouter),
    ]
    with ThreadPoolExecutor(max_workers=5) as pool:
        checks = list(pool.map(lambda item: check(*item), probes))
    releases = engine.releases()
    by_id = {release["id"]: release for release in releases}
    live, preview = engine.store.get("live"), engine.store.get("preview")
    installed = None
    if not live and (engine.config.site / "main/index.html").is_file():
        installed = dict(
            id="installed",
            commit="—",
            branch="Установленный сайт",
            created="—",
            snapshot="Метаданные релиза ещё не импортированы",
        )
    archived = []
    for release in releases:
        if release["id"] not in (live, preview):
            ports = [port for port in range(8522, 8530) if engine.store.get(f"archive-{port}") == release["id"]]
            archived.append({**release, **({"port": ports[0]} if ports else {})})
    return dict(
        services=services,
        checks=checks,
        storage=storage,
        error=error,
        backups=engine.backups(),
        live=by_id.get(live) or installed,
        preview=by_id.get(preview),
        archive=archived,
        publicRelease=engine.store.get("publicRelease"),
        pagesEnabled=bool(engine.config.pages_script and engine.config.pages_script.is_file()),
        schedule=engine.store.get("schedule", {"enabled": False, "time": "03:00", "keep": 3}),
    )
