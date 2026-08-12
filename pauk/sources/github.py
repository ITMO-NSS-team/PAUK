from __future__ import annotations

import time
from collections.abc import Iterator

import requests

from .base import HttpClient, HttpRequestError


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

        retry_after = self._parse_retry_after(response.headers.get("Retry-After"))
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
        try:
            self.get_json(f"{self.API_URL}/repos/{owner}/{name}/readme")
        except HttpRequestError as exc:
            if exc.status_code == 404:
                return False
            raise
        return True

    def _paged(self, url: str, pages: int, **params) -> Iterator[dict]:
        """Items from a paged endpoint, stopping at `pages` or the last page.

        GitHub caps per_page at 100 and answers a short page when the list
        ends, which is what ends the loop early.
        """
        for page in range(1, pages + 1):
            batch = self.get_json(url, params={"per_page": 100, "page": page, **params})
            if not batch:
                return
            yield from batch
            if len(batch) < 100:
                return

    def contributors(self, owner: str, name: str, pages: int = 1) -> list[dict]:
        """Accounts credited with commits to the repository.

        An empty list also comes back for a repository GitHub has not
        finished analysing, which is why callers treat it as "no data" and
        not as "nobody contributed".
        """
        return list(self._paged(f"{self.API_URL}/repos/{owner}/{name}/contributors", pages))

    def commits(self, owner: str, name: str, pages: int) -> list[dict]:
        """Recent commits, newest first.

        Each commit carries two identities: the GitHub account that owns it
        (`author.login`, absent when the commit email matches no account)
        and the git identity configured on the machine that made it
        (`commit.author`), which is where a personal email usually shows.
        """
        return list(self._paged(f"{self.API_URL}/repos/{owner}/{name}/commits", pages))

    def get_user(self, login: str) -> dict:
        return self.get_json(f"{self.API_URL}/users/{login}")
