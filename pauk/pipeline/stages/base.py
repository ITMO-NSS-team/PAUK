from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from time import monotonic
from typing import TypeVar

from tqdm import tqdm

from pauk.models.processing import ProcessingState, ProcessingStatus
from pauk.settings import Settings, settings
from pauk.storage import PreparedStore, RawStore

T = TypeVar("T")

#: Receives (label, done, total). The worker's reporter raises when a stop is
#: requested, so this is also where a stage can be cancelled.
OnProgress = Callable[[str, int, int], None]


@dataclass(frozen=True)
class PreparedSelection:
    entity: str
    ids: frozenset[str]


class EnrichmentStage(ABC):
    name: str
    progress_label: str | None = None

    #: Minimum seconds between hook calls; each call costs two Mongo round trips,
    #: so it is throttled by the clock rather than by row count.
    REPORT_SECONDS = 1.0

    def __init__(self, prepared: PreparedStore, raw: RawStore,
                 config: Settings | None = None,
                 selection: PreparedSelection | None = None,
                 force: bool = False,
                 on_progress: OnProgress | None = None) -> None:
        self.prepared = prepared
        self.raw = raw
        self.config = config or settings
        self.selection = selection
        self.force = force
        self.on_progress = on_progress

    def selected(self, entity: str, identifier: str) -> bool:
        return self.selection is None or (
            self.selection.entity == entity and identifier in self.selection.ids
        )

    def in_scope(self, entity: str, identifier: str) -> bool:
        """False only when the run is scoped to a list of *this* entity's ids
        that does not name this one.

        Unlike `selected`, a selection aimed at another entity does not filter
        here: a stage that reaches its rows through several entities decides
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
        """Iterate with a throttled progress bar when stderr is interactive.

        Also reports progress to the worker and checks for a stop request,
        which is safe between two items.
        """
        said = 0.0
        described = label or self.progress_label or self.name
        with tqdm(total=total, desc=described, unit=unit,
                  dynamic_ncols=True, disable=None) as bar:
            for done, item in enumerate(items, start=1):
                yield item
                bar.update()
                if self.on_progress is None:
                    continue
                if (moment := monotonic()) - said >= self.REPORT_SECONDS:
                    said = moment
                    self.on_progress(described, done, total)

    def progress_bar(self, *, total: int | None, label: str | None = None,
                     unit: str = "item") -> tqdm:
        """Create a throttled progress bar for work that is not iterable."""
        return tqdm(total=total, desc=label or self.progress_label or self.name, unit=unit,
                    dynamic_ncols=True, disable=None)

    @abstractmethod
    def run(self) -> dict[str, int]:
        raise NotImplementedError
