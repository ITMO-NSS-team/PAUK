from __future__ import annotations

import time

import requests

from .base import HttpClient, _parse_retry_after


class GitHubClient(HttpClient):
    API_URL = "https://api.github.com"

    def __init__(self, timeout: int, token: str = "") -> None:
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        super().__init__(timeout, headers)

    @staticmethod
    def _rate_limit_message(response: requests.Response) -> bool:
        try:
            payload = response.json()
        except ValueError:
            return False
        message = payload.get("message", "") if isinstance(payload, dict) else ""
        normalized = str(message).casefold()
        return "rate limit" in normalized or "abuse detection" in normalized

    def _retry_delay(self, response: requests.Response, attempt: int) -> float | None:
        delay = super()._retry_delay(response, attempt)
        if delay is not None or response.status_code != 403:
            return delay

        retry_after = _parse_retry_after(response.headers.get("Retry-After"))
        if retry_after is not None:
            return retry_after

        if response.headers.get("X-RateLimit-Remaining") == "0":
            try:
                reset_at = float(response.headers["X-RateLimit-Reset"])
            except (KeyError, TypeError, ValueError):
                return self._backoff_delay(attempt)
            return max(0.0, reset_at - time.time())

        if self._rate_limit_message(response):
            return max(60.0, self._backoff_delay(attempt))
        return None

    def get_repository(self, owner: str, name: str) -> dict:
        return self.get_json(f"{self.API_URL}/repos/{owner}/{name}")

    def has_readme(self, owner: str, name: str) -> bool:
        """Check for a README via GET .../readme (200 = yes, 404 = no).

        Not exposed by the repository payload itself, needs its own call.
        """
        response = self.session.get(f"{self.API_URL}/repos/{owner}/{name}/readme", timeout=self.timeout)
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return True
