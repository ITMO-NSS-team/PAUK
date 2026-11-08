from __future__ import annotations

from typing import Any

from pauk.settings import SEMANTIC_SCHOLAR_RPS

from .base import HttpClient
from .rate_limit import RateLimit

BATCH_URL = "https://api.semanticscholar.org/graph/v1/paper/batch"
MAX_BATCH = 500
BATCH_RETRIES = 8


class SemanticScholarClient(HttpClient):
    """Semantic Scholar Graph API; without a key the shared pool often answers 429."""

    def __init__(self, timeout: int, api_key: str = "", *,
                 requests_per_second: float = SEMANTIC_SCHOLAR_RPS) -> None:
        headers = {"User-Agent": "PAUK/2.0"}
        if api_key:
            headers["x-api-key"] = api_key
        super().__init__(timeout, headers,
                         rate_limits={"api.semanticscholar.org": RateLimit(requests_per_second, 1)})

    def papers_by_doi(self, dois: list[str]) -> list[dict[str, Any] | None]:
        """One record per DOI, in the same order; None where the DOI is unknown.

        Raises:
            ValueError: More than MAX_BATCH DOIs, or a response that is not
                one record per DOI.
            HttpRequestError: The request failed after retries.
        """
        if len(dois) > MAX_BATCH:
            raise ValueError(f"at most {MAX_BATCH} DOIs per batch, got {len(dois)}")
        records = self.request_json(
            "POST", BATCH_URL, params={"fields": "externalIds"},
            json={"ids": [f"DOI:{doi}" for doi in dois]}, retries=BATCH_RETRIES,
        )
        if not isinstance(records, list) or len(records) != len(dois):
            raise ValueError("Semantic Scholar batch response does not match the request")
        return records


def arxiv_id(record: dict[str, Any] | None) -> str | None:
    if not isinstance(record, dict):
        return None
    external = record.get("externalIds") or {}
    value = external.get("ArXiv") if isinstance(external, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None
