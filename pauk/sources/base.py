from __future__ import annotations

import time
from typing import Any

import requests


class HttpClient:
    def __init__(self, timeout: int, headers: dict[str, str] | None = None) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(headers or {})

    def _get(self, url: str, *, params: dict[str, Any] | None = None,
              retries: int = 3) -> requests.Response:
        for attempt in range(retries + 1):
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
                if response.status_code in {429, 500, 502, 503, 504} and attempt < retries:
                    retry_after = response.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after and retry_after.isdigit() else min(60, 2 ** attempt)
                    time.sleep(delay)
                    continue
                response.raise_for_status()
                return response
            except requests.RequestException:
                if attempt == retries:
                    raise
                time.sleep(min(60, 2 ** attempt))
        raise RuntimeError(f"unreachable retry state for {url}")

    def get_json(self, url: str, *, params: dict[str, Any] | None = None,
                 retries: int = 3) -> dict[str, Any]:
        return self._get(url, params=params, retries=retries).json()

    def get_bytes(self, url: str, *, retries: int = 3) -> bytes:
        return self._get(url, retries=retries).content
