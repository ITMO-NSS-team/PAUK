from __future__ import annotations

from tqdm.contrib.logging import logging_redirect_tqdm

from pauk.pipeline.stages import ALL_STAGES, OPTIONAL_STAGES
from pauk.pipeline.stages.base import PreparedSelection
from pauk.settings import Settings, settings
from pauk.storage import PreparedStore, RawStore


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
            force: bool = False) -> dict[str, int]:
        if stage_name not in (None, "all") and stage_name not in self.stages:
            available = ", ".join(self.stages)
            raise ValueError(f"unknown enrichment stage {stage_name!r}; choose one of: {available}, all")
        classes = ALL_STAGES if stage_name in (None, "all") else (self.stages[stage_name],)
        result: dict[str, int] = {}
        # No GroupLock here (unlike origin/main pre-Mongo): removed in #102,
        # see the matching note in pauk/pipeline/collect.py::collect.
        with logging_redirect_tqdm():
            for stage_class in classes:
                for key, value in stage_class(self.prepared, self.raw, self.config, selection, force).run().items():
                    result[key] = result.get(key, 0) + value
        return result
