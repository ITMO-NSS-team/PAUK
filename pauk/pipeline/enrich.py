from __future__ import annotations

from collections.abc import Callable

from tqdm.contrib.logging import logging_redirect_tqdm

from pauk.pipeline.stages import ALL_STAGES, OPTIONAL_STAGES
from pauk.pipeline.stages.base import OnProgress, PreparedSelection
from pauk.settings import Settings, settings
from pauk.storage import PreparedStore, RawStore

#: Called before each stage with its name, how many stages are done, and the total.
OnStage = Callable[[str, int, int], None]


def _inside(on_stage: OnStage | None, done: int, total: int) -> OnProgress | None:
    """The reporter a stage gets, reporting from inside one stage.

    Run counts are stages, not rows (the page reads them as "stage 3 of 10"),
    so a stage's own progress goes into the label and the counts stay per stage.
    """
    if on_stage is None:
        return None

    def inside(label: str, rows_done: int, rows_total: int) -> None:
        told = f"{label} {rows_done}/{rows_total}" if rows_total else label
        on_stage(told, done, total)

    return inside


class Enricher:
    def __init__(self, prepared: PreparedStore, raw: RawStore,
                 config: Settings | None = None) -> None:
        self.prepared = prepared
        self.raw = raw
        self.config = config or settings
        # Optional stages run only when named: they are expensive and
        # depend on what an earlier run confirmed, so a full pass must
        # not drag them along.
        self.stages = {stage.name: stage for stage in (*ALL_STAGES, *OPTIONAL_STAGES)}

    def run(self, stage_name: str | None = None,
            selection: PreparedSelection | None = None,
            force: bool = False, on_stage: OnStage | None = None) -> dict[str, int]:
        if stage_name not in (None, "all") and stage_name not in self.stages:
            available = ", ".join(self.stages)
            raise ValueError(f"unknown enrichment stage {stage_name!r}; choose one of: {available}, all")
        classes = ALL_STAGES if stage_name in (None, "all") else (self.stages[stage_name],)
        result: dict[str, int] = {}
        with logging_redirect_tqdm():
            for done, stage_class in enumerate(classes):
                if on_stage is not None:
                    on_stage(stage_class.name, done, len(classes))
                stage = stage_class(self.prepared, self.raw, self.config, selection, force,
                                    on_progress=_inside(on_stage, done, len(classes)))
                for key, value in stage.run().items():
                    result[key] = result.get(key, 0) + value
        return result
