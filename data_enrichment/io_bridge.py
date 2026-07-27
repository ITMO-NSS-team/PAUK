"""Источник и сток конвейера. Оба края — JSONL, SQL здесь нет.

JSONL: файл на тип сущности, объект на строку. Пишется потоково по ходу
обработки с flush на строку — при падении пайплайна уже обработанное не
теряется. Входные data/input/*.jsonl пока готовит разовый scripts/export_input
из SQLite; сток пишет обогащённые сущности так же. Загрузка в Neo4j — зона
коннектора.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel

from .config import DATA_DIR
from .conveyor import PipelinePerson, PubUnit, to_json
from .models import Department

logger = logging.getLogger(__name__)

INPUT_DIR = Path(DATA_DIR) / "input"


def _read_jsonl(path: Path, limit: int | None = None) -> Iterator[dict]:
    with path.open(encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if limit is not None and i >= limit:
                return
            if line.strip():
                yield json.loads(line)


def read_persons(limit: int | None = None) -> Iterator[PipelinePerson]:
    for row in _read_jsonl(INPUT_DIR / "persons.jsonl", limit):
        yield PipelinePerson(**row)


def read_publications(limit: int | None = None) -> Iterator[PubUnit]:
    for row in _read_jsonl(INPUT_DIR / "publications.jsonl", limit):
        yield PubUnit(**row)


def read_departments(limit: int | None = None) -> Iterator[Department]:
    for row in _read_jsonl(INPUT_DIR / "departments.jsonl", limit):
        yield Department(**row)


def read_candidate_rows(limit: int | None = None) -> Iterator[dict]:
    yield from _read_jsonl(INPUT_DIR / "github_candidates.jsonl", limit)


def read_itmo_orgs() -> set[str]:
    return {r["github_login"].lower() for r in _read_jsonl(INPUT_DIR / "github_orgs.jsonl")
            if r.get("github_login")}


class JsonlSink:
    """Сток: JSONL с flush на каждую строку — обработанное переживает падение."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w", encoding="utf-8")
        self.count = 0

    def _write_line(self, line: str) -> None:
        self._fh.write(line + "\n")
        self._fh.flush()
        self.count += 1

    def write_person(self, person: PipelinePerson) -> None:
        self._write_line(to_json(person))

    def write_model(self, obj: BaseModel) -> None:
        self._write_line(obj.model_dump_json(exclude_none=True))

    def close(self) -> None:
        self._fh.close()
        logger.info("сток %s: записано %d", self.path.name, self.count)
