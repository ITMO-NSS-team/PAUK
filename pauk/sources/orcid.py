from .base import HttpClient


class OrcidClient(HttpClient):
    def __init__(self, timeout: int) -> None:
        super().__init__(timeout, {"Accept": "application/json"})

    def get_record(self, orcid: str) -> dict:
        return self.get_json(f"https://pub.orcid.org/v3.0/{orcid}/record")
