from pauk.settings import ORCID_RPS

from .base import HttpClient
from .rate_limit import RateLimit


class OrcidClient(HttpClient):
    def __init__(self, timeout: int, *, requests_per_second: float = ORCID_RPS) -> None:
        super().__init__(
            timeout, {"Accept": "application/json"},
            rate_limits={"pub.orcid.org": RateLimit(requests_per_second)},
        )

    def get_record(self, orcid: str) -> dict:
        return self.get_json(f"https://pub.orcid.org/v3.0/{orcid}/record")
