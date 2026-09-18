from __future__ import annotations

from pymongo.database import Database

from pauk.jobs.locks import held
from pauk.pipeline.collect import Collector
from pauk.pipeline.enrich import Enricher
from pauk.pipeline.normalize import OpenAlexNormalizer
from pauk.pipeline.selectors import PeriodSelector, WorkSelector, WorksFileSelector
from pauk.settings import Settings
from pauk.sources import OpenAlexClient
from pauk.storage import PreparedStore, RawStore


class PipelineRunner:
    def __init__(self, config: Settings, group: str, mongo_db: Database) -> None:
        self.config = config
        self.group = group
        self.db = mongo_db
        self.raw = RawStore(mongo_db, group)
        self.prepared = PreparedStore(mongo_db, group)

    def run(self, selector: WorkSelector | PeriodSelector | WorksFileSelector) -> dict[str, int]:
        """Collect, normalize and enrich one group.

        Holds the group for the whole run. Every stage reads its group's
        full working set and writes it back, so two runs over the same
        group overwrite each other's rows — the same reason publishing
        holds the graph.

        Raises:
            Busy: Another run is already working on this group.
        """
        with held(self.db, f"group:{self.group}"):
            return self._run(selector)

    def _run(self, selector: WorkSelector | PeriodSelector | WorksFileSelector) -> dict[str, int]:
        client = OpenAlexClient(self.config.request_timeout, self.config.openalex_api_key)
        count = Collector(client, self.raw).collect(selector)
        result = {"raw_works": count}
        result.update(OpenAlexNormalizer(self.raw, self.prepared).run())
        result.update(Enricher(self.prepared, self.raw, self.config).run())
        return result
