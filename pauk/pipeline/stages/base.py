from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from pauk.models.processing import ProcessingState, ProcessingStatus
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
                 selection: PreparedSelection | None = None,
                 force: bool = False) -> None:
        self.prepared = prepared
        self.raw = raw
        self.config = config or settings
        self.selection = selection
        self.force = force

    def selected(self, entity: str, identifier: str) -> bool:
        return self.selection is None or (
            self.selection.entity == entity and identifier in self.selection.ids
        )

    def needs_attempt(self, state: ProcessingState | None) -> bool:
        """True if the stage should (re)process a row in this state.

        Normally completed rows are skipped; with force=True every selected
        row is reprocessed (e.g. after a bug fix or to re-judge with a newly
        configured external service).
        """
        if self.force:
            return True
        return state is None or state.status in {ProcessingStatus.NOT_STARTED, ProcessingStatus.FAILED}

    @abstractmethod
    def run(self) -> dict[str, int]:
        raise NotImplementedError
