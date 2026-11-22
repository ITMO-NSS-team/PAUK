from __future__ import annotations

import re
import threading
from collections.abc import Iterator, Sequence

from pauk.aliases import flatten_merges
from pauk.models.identity import normalize_external_value
from pauk.settings import OPENALEX_PUBLIC_RPS, OPENALEX_RPS

from .base import HttpClient
from .rate_limit import RateLimit

AUTHOR_BATCH_SIZE = 100


def normalize_author_id(author_id: str) -> str:
    if not isinstance(author_id, str):
        raise ValueError("Invalid OpenAlex author ID")
    normalized = normalize_external_value("openalex", author_id)
    if not re.fullmatch(r"A[0-9]+", normalized):
        raise ValueError("Invalid OpenAlex author ID")
    return normalized


class OpenAlexClient(HttpClient):
    WORKS_URL = "https://api.openalex.org/works"
    AUTHORS_URL = "https://api.openalex.org/authors"

    def __init__(self, timeout: int, api_key: str = "", *, requests_per_second: float = OPENALEX_RPS,
                 cancel: threading.Event | None = None) -> None:
        self.api_key = api_key
        limit = RateLimit(requests_per_second)
        if not api_key:
            limit = limit.stricter(RateLimit(OPENALEX_PUBLIC_RPS))
        headers = {"User-Agent": "PAUK/2.0"}
        if self.api_key:
            # Same-host redirects retain Authorization but replace query parameters.
            headers["Authorization"] = f"Bearer {api_key}"
        super().__init__(timeout, headers, rate_limits={"api.openalex.org": limit}, cancel=cancel)

    @staticmethod
    def normalize_work_id(work_id: str) -> str:
        return work_id.rstrip("/").split("/")[-1].upper()

    def get_work(self, work_id: str) -> dict:
        normalized = self.normalize_work_id(work_id)
        return self.get_json(f"{self.WORKS_URL}/{normalized}")

    def iter_works(self, ror_id: str, date_from: str, date_to: str) -> Iterator[dict]:
        cursor = "*"
        # Inclusive bounds via from_/to_publication_date, so boundary dates are not lost.
        filters = (
            f"authorships.institutions.ror:{ror_id},"
            f"from_publication_date:{date_from},to_publication_date:{date_to}"
        )
        while cursor:
            page = self.get_json(self.WORKS_URL, params=dict(
                filter=filters, sort="publication_date:desc", per_page=100, cursor=cursor,
            ))
            yield from page.get("results", [])
            cursor = page.get("meta", {}).get("next_cursor")

    def get_author(self, author_id: str) -> dict:
        normalized = normalize_author_id(author_id)
        return self.get_json(f"{self.AUTHORS_URL}/{normalized}")

    def get_authors(self, author_ids: Sequence[str]) -> dict[str, dict]:
        """Map a batch response back to requested IDs, including merged authors."""
        identifiers = list(dict.fromkeys(normalize_author_id(identifier) for identifier in author_ids))
        if not identifiers:
            return {}
        if len(identifiers) > AUTHOR_BATCH_SIZE:
            raise ValueError(f"OpenAlex author batches cannot exceed {AUTHOR_BATCH_SIZE} IDs")
        response = self.get_json(self.AUTHORS_URL, params={
            "filter": "openalex_id:" + "|".join(identifiers), "per_page": AUTHOR_BATCH_SIZE,
        })
        if not isinstance(response, dict) or not isinstance(response.get("results"), list):
            raise ValueError("Invalid OpenAlex author batch response")
        records: dict[str, dict] = {}
        conflicts: set[str] = set()
        for record in response["results"]:
            if not isinstance(record, dict):
                continue
            try:
                identifier = normalize_author_id(record.get("id"))
            except ValueError:
                continue
            if records.setdefault(identifier, record) != record:
                conflicts.add(identifier)
        metadata = response.get("meta", {})
        if not isinstance(metadata, dict) or not isinstance(metadata.get("merged_ids", []), list):
            raise ValueError("Invalid OpenAlex author aliases")
        aliases = []
        ambiguous: set[str] = set()
        for merge in metadata.get("merged_ids", []):
            if not isinstance(merge, dict):
                continue
            try:
                source = normalize_author_id(merge.get("id"))
            except ValueError:
                continue
            try:
                target = normalize_author_id(merge.get("merge_into_id"))
            except ValueError:
                ambiguous.add(source)
            else:
                aliases.append((source, target))
        resolved = dict(flatten_merges(aliases, ambiguous=ambiguous))
        return {identifier: records[canonical] for identifier in identifiers if identifier not in ambiguous
                and (canonical := resolved.get(identifier, identifier)) in records and canonical not in conflicts}
