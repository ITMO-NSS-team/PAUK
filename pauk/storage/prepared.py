from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from .atomic import AtomicWriter


M = TypeVar("M", bound=BaseModel)


class PreparedStore:
    FILES = {
        "publications": "publications.jsonl",
        "persons": "persons.jsonl",
        "departments": "departments.jsonl",
        "repositories": "repositories.jsonl",
        "github_profiles": "github_profiles.jsonl",
        "repo_links": "repo_links.jsonl",
    }

    def __init__(self, root: Path, group: str) -> None:
        self.group_dir = root / group

    def path_for(self, entity: str) -> Path:
        try:
            return self.group_dir / self.FILES[entity]
        except KeyError as exc:
            raise ValueError(f"unknown prepared entity: {entity}") from exc

    @classmethod
    def entity_for_path(cls, path: Path) -> str:
        for entity, filename in cls.FILES.items():
            if path.name == filename:
                return entity
        raise ValueError(f"not a prepared entity file: {path}")

    def read_rows(self, entity: str) -> Iterator[dict]:
        path = self.path_for(entity)
        if not path.exists():
            return
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    yield json.loads(line)

    def read_models(self, entity: str, model: type[M]) -> Iterator[M]:
        for row in self.read_rows(entity):
            yield model.model_validate(row)

    def write_models(self, entity: str, rows: Iterable[BaseModel]) -> None:
        with AtomicWriter(self.path_for(entity)) as fh:
            for row in rows:
                fh.write(row.model_dump_json(by_alias=True, exclude_none=True) + "\n")

    def write_rows(self, entity: str, rows: Iterable[dict]) -> None:
        with AtomicWriter(self.path_for(entity)) as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
