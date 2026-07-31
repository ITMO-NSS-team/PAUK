from __future__ import annotations

from pauk.pipeline.selectors import PeriodSelector, WorkSelector, WorksFileSelector
from pauk.sources import OpenAlexClient
from pauk.storage import GroupLock, RawStore


ITMO_ROR_ID = "04txgxn49"


class Collector:
    def __init__(self, client: OpenAlexClient, raw: RawStore) -> None:
        self.client = client
        self.raw = raw

    def collect(self, selector: WorkSelector | PeriodSelector | WorksFileSelector) -> int:
        with GroupLock(self.raw.group_dir.parent.parent, self.raw.group_dir.name):
            return self._collect(selector)

    def _collect(self, selector: WorkSelector | PeriodSelector | WorksFileSelector) -> int:
        known_ids = {
            (row.get("payload", {}).get("id") or "").rstrip("/").split("/")[-1].upper()
            for row in self.raw.read("openalex_works")
        }
        if isinstance(selector, WorkSelector):
            if selector.work_id.rstrip("/").split("/")[-1].upper() in known_ids:
                return 0
            work = self.client.get_work(selector.work_id)
            self.raw.append("openalex_works", work, {"work_id": selector.work_id})
            return 1
        if isinstance(selector, WorksFileSelector):
            count = 0
            for work_id in selector.ids():
                if work_id.rstrip("/").split("/")[-1].upper() in known_ids:
                    continue
                work = self.client.get_work(work_id)
                self.raw.append("openalex_works", work, {"work_id": work_id})
                known_ids.add(work_id.rstrip("/").split("/")[-1].upper())
                count += 1
            return count
        count = 0
        for work in self.client.iter_works(ITMO_ROR_ID, selector.date_from, selector.date_to):
            work_id = (work.get("id") or "").rstrip("/").split("/")[-1].upper()
            if work_id in known_ids:
                continue
            self.raw.append("openalex_works", work, {
                "from": selector.date_from, "to": selector.date_to,
            })
            known_ids.add(work_id)
            count += 1
        return count
