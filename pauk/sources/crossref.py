import re
from urllib.parse import quote

from pauk.settings import (
    CROSSREF_CONCURRENCY,
    CROSSREF_PUBLIC_CONCURRENCY,
    CROSSREF_PUBLIC_RPS,
    CROSSREF_RPS,
)

from .base import HttpClient
from .rate_limit import RateLimit


class CrossrefClient(HttpClient):
    def __init__(
        self, timeout: int, mailto: str = "", *, requests_per_second: float = CROSSREF_RPS,
        max_concurrent: int = CROSSREF_CONCURRENCY,
    ) -> None:
        self.mailto = mailto.strip()
        if self.mailto and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", self.mailto):
            raise ValueError("Crossref contact must be an email address")
        limit = RateLimit(requests_per_second, max_concurrent)
        if not self.mailto:
            limit = limit.stricter(RateLimit(CROSSREF_PUBLIC_RPS, CROSSREF_PUBLIC_CONCURRENCY))
        # Keep polite routing when a redirect drops the mailto query parameter.
        agent = f"PAUK/2.0 (mailto:{self.mailto})" if self.mailto else "PAUK/2.0"
        super().__init__(timeout, {"User-Agent": agent}, rate_limits={"api.crossref.org": limit})

    def get_work(self, doi: str) -> dict:
        clean = doi.removeprefix("https://doi.org/").removeprefix("http://dx.doi.org/")
        params = {"mailto": self.mailto} if self.mailto else None
        return self.get_json(f"https://api.crossref.org/works/{quote(clean, safe='/')}", params=params)
