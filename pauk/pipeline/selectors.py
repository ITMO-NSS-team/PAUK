from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class WorkSelector:
    work_id: str


@dataclass(frozen=True)
class PeriodSelector:
    date_from: str
    date_to: str

    def __post_init__(self) -> None:
        if date.fromisoformat(self.date_from) > date.fromisoformat(self.date_to):
            raise ValueError("--from must not be later than --to")


@dataclass(frozen=True)
class WorksFileSelector:
    path: Path

    def ids(self) -> list[str]:
        if not self.path.is_file():
            raise FileNotFoundError(f"works file not found: {self.path}")
        return [line.strip() for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]
