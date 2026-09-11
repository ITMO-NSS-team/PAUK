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

#: Told where a stage has got to, by label and by how many of how many. The
#: worker passes its own reporter, which raises when somebody has asked the
#: run to stop, so this is also where a stage can be given up.
OnProgress = Callable[[str, int, int], None]


@dataclass(frozen=True)
class PreparedSelection:
    entity: str
    ids: frozenset[str]


class EnrichmentStage(ABC):
    name: str
    progress_label: str | None = None

    #: How often `progress` may call the hook. A stage can run through
    #: thousands of cheap rows a second and every call is two round trips
    #: to Mongo, so it is throttled by the clock rather than by a count.
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

        Also where a run under the worker says how far it has got and finds
        out it has been asked to stop. Between two items is the safe place
        for both: the one just handed out has been dealt with, and the next
        has not been touched.
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
