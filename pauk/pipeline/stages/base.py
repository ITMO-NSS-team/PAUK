from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from pauk.settings import Settings, settings
from pauk.storage import PreparedStore, RawStore


@dataclass(frozen=True)
class PreparedSelection:
    entity: str
    ids: frozenset[str]


class EnrichmentStage(ABC):
    name: str

    def __init__(self, prepared: PreparedStore, raw: RawStore,
                 config: Settings | None = None,
                 selection: PreparedSelection | None = None) -> None:
        self.prepared = prepared
        self.raw = raw
        self.config = config or settings
        self.selection = selection

    def selected(self, entity: str, identifier: str) -> bool:
        return self.selection is None or (
            self.selection.entity == entity and identifier in self.selection.ids
        )

    @abstractmethod
    def run(self) -> dict[str, int]:
        raise NotImplementedError
