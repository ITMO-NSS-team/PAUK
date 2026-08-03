from __future__ import annotations

import logging

from pauk.pipeline.selectors import PeriodSelector, WorkSelector, WorksFileSelector
from pauk.sources import OpenAlexClient
from pauk.storage import GroupLock, RawStore

logger = logging.getLogger(__name__)

ITMO_ROR_ID = "04txgxn49"

# OpenAlex serves at most this many authorships on the list endpoint (large
# consortium papers have hundreds), without any marker field; the single-work
# endpoint serves the complete list. A list payload carrying exactly the cap
# is treated as truncated — the ITMO participant is often beyond it.
AUTHORSHIP_TRUNCATION_LIMIT = 100

# Request marker on envelopes whose payload came from the single-work
# endpoint: their author list is complete even when it is exactly at the
# cap, so the repair pass must not re-fetch them forever.
FULL_AUTHOR_LIST = "full_author_list"


def _authors_truncated(work: dict) -> bool:
    return bool(work.get("is_authors_truncated")) or \
        len(work.get("authorships") or []) == AUTHORSHIP_TRUNCATION_LIMIT


class Collector:
    def __init__(self, client: OpenAlexClient, raw: RawStore) -> None:
        self.client = client
        self.raw = raw

    def collect(self, selector: WorkSelector | PeriodSelector | WorksFileSelector) -> int:
        with GroupLock(self.raw.group_dir.parent.parent, self.raw.group_dir.name):
            return self._collect(selector)

    def refetch_truncated(self) -> int:
        """Re-fetch full author lists for stored works that carry truncated
        ones. Runs as part of every collect; callable on its own to repair a
        group collected before truncation was handled, without re-crawling
        the whole period."""
        with GroupLock(self.raw.group_dir.parent.parent, self.raw.group_dir.name):
            return self._refetch_truncated(self._last_payload_by_id())

    def _last_payload_by_id(self) -> dict[str, tuple[dict, dict]]:
        """The latest stored (payload, request) per work id — a repair appends
        a second envelope for a work, and the latest one is authoritative."""
        rows: dict[str, tuple[dict, dict]] = {}
        for row in self.raw.read("openalex_works"):
            payload = row.get("payload") or {}
            work_id = (payload.get("id") or "").rstrip("/").split("/")[-1].upper()
            if work_id:
                rows[work_id] = (payload, row.get("request") or {})
        return rows

    def _refetch_truncated(self, rows: dict[str, tuple[dict, dict]]) -> int:
        count = 0
        for work_id, (payload, request) in rows.items():
            # Envelopes fetched from the single-work endpoint are complete
            # even at exactly the cap.
            if request.get("refetch") == FULL_AUTHOR_LIST or "work_id" in request:
                continue
            if not _authors_truncated(payload):
                continue
            full = self.client.get_work(work_id)
            self.raw.append("openalex_works", full, {
                "work_id": work_id, "refetch": FULL_AUTHOR_LIST,
            })
            count += 1
        if count:
            logger.info("collect: re-fetched the full author list of %d truncated work(s)", count)
        return count

    def _collect(self, selector: WorkSelector | PeriodSelector | WorksFileSelector) -> int:
        rows = self._last_payload_by_id()
        known_ids = set(rows)
        self._refetch_truncated(rows)
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
            request = {"from": selector.date_from, "to": selector.date_to}
            if _authors_truncated(work):
                # The list endpoint cut the author list; the single-work
                # endpoint has all of it.
                work = self.client.get_work(work_id)
                request["refetch"] = FULL_AUTHOR_LIST
            self.raw.append("openalex_works", work, request)
            known_ids.add(work_id)
            count += 1
        return count
