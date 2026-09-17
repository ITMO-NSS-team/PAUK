from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import TypeVar

from tqdm import tqdm

from pauk.models.processing import ProcessingState, ProcessingStatus
from pauk.settings import Settings, settings
from pauk.storage import PreparedStore, RawStore

T = TypeVar("T")


@dataclass(frozen=True)
class PreparedSelection:
    entity: str
    ids: frozenset[str]


class EnrichmentStage(ABC):
    name: str
    progress_label: str | None = None

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

    def in_scope(self, entity: str, identifier: str) -> bool:
        """False only when the run is scoped to a list of *this* entity's ids
        that does not name this one.

        Unlike `selected`, a selection aimed at another entity does not filter
        here — a stage that reaches its rows through several entities decides
        for itself what a publication-scoped run means for each of them.
        """
        return (self.selection is None
                or self.selection.entity != entity
                or identifier in self.selection.ids)

    def needs_attempt(self, state: ProcessingState | None) -> bool:
        """True if the stage should (re)process a row in this state.

        Normally completed rows are skipped; with force=True every selected
        row is reprocessed (e.g. after a bug fix or to re-judge with a newly
        configured external service).
        """
        if self.force:
            return True
        return state is None or state.status in {ProcessingStatus.NOT_STARTED, ProcessingStatus.FAILED}

    def progress(self, items: Iterable[T], *, total: int,
                 label: str | None = None, unit: str = "item") -> Iterator[T]:
        """Iterate with a throttled progress bar when stderr is interactive."""
        with tqdm(total=total, desc=label or self.progress_label or self.name, unit=unit,
                  dynamic_ncols=True, disable=None) as bar:
            for item in items:
                yield item
                bar.update()

    def progress_bar(self, *, total: int | None, label: str | None = None,
                     unit: str = "item") -> tqdm:
        """Create a throttled progress bar for work that is not iterable."""
        return tqdm(total=total, desc=label or self.progress_label or self.name, unit=unit,
                    dynamic_ncols=True, disable=None)

    @abstractmethod
    def run(self) -> dict[str, int]:
        raise NotImplementedError
