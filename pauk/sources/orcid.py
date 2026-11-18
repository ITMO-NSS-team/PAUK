import math
import re
import threading
from dataclasses import dataclass
from time import monotonic
from urllib.parse import quote

from pauk.settings import ORCID_RPS

from .base import HttpClient, HttpRequestError
from .rate_limit import RateLimit

TOKEN_URL = "https://orcid.org/oauth/token"


class OrcidAuthenticationError(RuntimeError):
    """Configured ORCID credentials could not authorize public reads."""


@dataclass(frozen=True)
class _Token:
    value: str
    expires_at: float


class OrcidClient(HttpClient):
    """Public records with a shared read-public token for each credential pair."""

    _tokens: dict[tuple[str, str], _Token] = {}
    _lock = threading.Lock()

    def __init__(self, timeout: int, client_id: str = "", client_secret: str = "", *,
                 requests_per_second: float = ORCID_RPS, cancel: threading.Event | None = None) -> None:
        if bool(client_id) != bool(client_secret):
            raise ValueError("ORCID_CLIENT_ID and ORCID_CLIENT_SECRET must be set together")
        self._credentials = (client_id, client_secret) if client_id else None
        super().__init__(
            timeout, {"Accept": "application/json"},
            rate_limits={"pub.orcid.org": RateLimit(requests_per_second)},
            cancel=cancel,
        )

    def authorize(self) -> None:
        """Authorize this session lazily, sharing token requests between threads."""
        if self._credentials is None:
            return
        with self._lock:
            token = self._tokens.get(self._credentials)
            if token is None or token.expires_at <= monotonic():
                token = self._read_token(*self._credentials)
                self._tokens[self._credentials] = token
        self.session.headers["Authorization"] = f"Bearer {token.value}"

    def _read_token(self, client_id: str, client_secret: str) -> _Token:
        client = HttpClient(self.timeout, {"Accept": "application/json"}, cancel=self._cancel)
        started = monotonic()
        try:
            with client._request("POST", TOKEN_URL, allow_redirects=False, data={
                "client_id": client_id, "client_secret": client_secret,
                "grant_type": "client_credentials", "scope": "/read-public",
            }) as response:
                if response.status_code != 200:
                    raise OrcidAuthenticationError(f"ORCID token endpoint returned HTTP {response.status_code}")
                payload = response.json()
            if not isinstance(payload, dict):
                raise OrcidAuthenticationError("Invalid ORCID token response")
            value, expires = payload.get("access_token"), payload.get("expires_in")
            if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", value)
                    or str(payload.get("token_type", "")).casefold() != "bearer"
                    or isinstance(expires, bool) or not isinstance(expires, (int, float))
                    or not math.isfinite(expires) or expires <= 0):
                raise OrcidAuthenticationError("Invalid ORCID token response")
            return _Token(value, started + expires)
        except HttpRequestError as error:
            status = f" (HTTP {error.status_code})" if error.status_code is not None else ""
            raise OrcidAuthenticationError(f"ORCID token request failed{status}") from None
        except (ValueError, OverflowError):
            raise OrcidAuthenticationError("Invalid ORCID token response") from None
        finally:
            client.close()

    def get_record(self, orcid: str) -> dict:
        self.authorize()
        try:
            return self.get_json(f"https://pub.orcid.org/v3.0/{quote(orcid, safe='')}/record")
        except HttpRequestError as error:
            if self._credentials is not None and error.status_code == 401:
                with self._lock:
                    token = self._tokens.get(self._credentials)
                    if token and self.session.headers.get("Authorization") == f"Bearer {token.value}":
                        del self._tokens[self._credentials]
                raise OrcidAuthenticationError("ORCID rejected the configured access token") from None
            raise
