import threading

from .base import HttpClient

TOKEN_URL = "https://orcid.org/oauth/token"


class OrcidClient(HttpClient):
    """Public ORCID records, anonymous or as a registered public API client.

    Anonymous reads are capped at 25,000 a day per IP address; a registered
    client gets 100,000 a day. Its read-public token lasts for years, so one
    is requested per process and shared by every client and thread.
    """

    _tokens: dict[str, str] = {}
    _lock = threading.Lock()

    def __init__(self, timeout: int, client_id: str = "", client_secret: str = "") -> None:
        super().__init__(timeout, {"Accept": "application/json"})
        if client_id and client_secret:
            self.session.headers["Authorization"] = f"Bearer {self._token(client_id, client_secret)}"

    def _token(self, client_id: str, client_secret: str) -> str:
        with self._lock:
            if client_id not in self._tokens:
                response = self._request("POST", TOKEN_URL, data={
                    "client_id": client_id, "client_secret": client_secret,
                    "grant_type": "client_credentials", "scope": "/read-public",
                })
                self._tokens[client_id] = response.json()["access_token"]
            return self._tokens[client_id]

    def get_record(self, orcid: str) -> dict:
        return self.get_json(f"https://pub.orcid.org/v3.0/{orcid}/record")
