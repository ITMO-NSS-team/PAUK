from __future__ import annotations

from collections.abc import Iterator

from pauk.settings import OPENALEX_PUBLIC_RPS, OPENALEX_RPS

from .base import HttpClient
from .rate_limit import RateLimit


class OpenAlexClient(HttpClient):
    WORKS_URL = "https://api.openalex.org/works"
    AUTHORS_URL = "https://api.openalex.org/authors"

    def __init__(self, timeout: int, api_key: str = "", *, requests_per_second: float = OPENALEX_RPS) -> None:
        self.api_key = api_key
        limit = RateLimit(requests_per_second)
        if not api_key:
            limit = limit.stricter(RateLimit(OPENALEX_PUBLIC_RPS))
        headers = {"User-Agent": "PAUK/2.0"}
        if self.api_key:
            # Same-host redirects retain Authorization but replace query parameters.
            headers["Authorization"] = f"Bearer {api_key}"
        super().__init__(timeout, headers, rate_limits={"api.openalex.org": limit})

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
        normalized = author_id.rstrip("/").split("/")[-1].upper()
        return self.get_json(f"{self.AUTHORS_URL}/{normalized}")

