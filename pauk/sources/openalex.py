from __future__ import annotations

from collections.abc import Iterator

from .base import HttpClient


class OpenAlexClient(HttpClient):
    WORKS_URL = "https://api.openalex.org/works"
    AUTHORS_URL = "https://api.openalex.org/authors"

    def __init__(self, timeout: int, api_key: str = "") -> None:
        super().__init__(timeout, {"User-Agent": "PAUK/2.0"})
        self.api_key = api_key

    def _params(self, **params: str | int) -> dict:
        if self.api_key:
            params["api_key"] = self.api_key
        return params

    @staticmethod
    def normalize_work_id(work_id: str) -> str:
        return work_id.rstrip("/").split("/")[-1].upper()

    def get_work(self, work_id: str) -> dict:
        normalized = self.normalize_work_id(work_id)
        return self.get_json(f"{self.WORKS_URL}/{normalized}", params=self._params())

    def iter_works(self, ror_id: str, date_from: str, date_to: str) -> Iterator[dict]:
        cursor = "*"
        # OpenAlex supports inclusive date filters; this fixes the old > / < boundary bug.
        filters = (
            f"authorships.institutions.ror:{ror_id},"
            f"from_publication_date:{date_from},to_publication_date:{date_to}"
        )
        while cursor:
            page = self.get_json(self.WORKS_URL, params=self._params(
                filter=filters, sort="publication_date:desc", per_page=100, cursor=cursor,
            ))
            yield from page.get("results", [])
            cursor = page.get("meta", {}).get("next_cursor")

    def get_author(self, author_id: str) -> dict:
        normalized = author_id.rstrip("/").split("/")[-1].upper()
        return self.get_json(f"{self.AUTHORS_URL}/{normalized}", params=self._params())

