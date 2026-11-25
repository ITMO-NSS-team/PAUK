from __future__ import annotations

import base64
import logging
import re
import time
from collections.abc import Callable, Iterator
from typing import Literal

import requests
from pydantic import BaseModel, ConfigDict

from .base import HttpClient, HttpRequestError

MAX_README_BYTES = 2 * 1024 * 1024
logger = logging.getLogger(__name__)


class Readme(BaseModel):
    """A README can exist even when the Contents API omits its bytes."""

    model_config = ConfigDict(frozen=True, ser_json_bytes="base64", val_json_bytes="base64")
    status: Literal["available", "empty", "missing", "too_large", "unavailable", "invalid"]
    path: str = ""
    sha: str = ""
    body: bytes = b""


def _decode_readme(payload: dict, path: str, sha: str) -> Readme:
    content = payload.get("content")
    size = payload.get("size")
    meta = {"path": path, "sha": sha}
    if isinstance(size, int) and size > MAX_README_BYTES:
        return Readme(status="too_large", **meta)
    if payload.get("encoding") != "base64" or not isinstance(content, str) or (not content and size != 0):
        return Readme(status="unavailable", **meta)
    encoded = "".join(content.split())
    if len(encoded) > 4 * ((MAX_README_BYTES + 2) // 3):
        return Readme(status="too_large", **meta)
    try:
        body = base64.b64decode(encoded, validate=True)
    except ValueError:
        return Readme(status="invalid", **meta)
    if len(body) > MAX_README_BYTES:
        return Readme(status="too_large", **meta)
    if isinstance(size, int) and len(body) != size:
        return Readme(status="invalid", **meta)
    return Readme(status="available" if body else "empty", body=body, **meta)


class GitHubClient(HttpClient):
    API_URL = "https://api.github.com"

    def __init__(self, timeout: int, token: str = "") -> None:
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        super().__init__(timeout, headers)

    @staticmethod
    def _error_message(response: requests.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return ""
        return str(payload.get("message", "") if isinstance(payload, dict) else "").casefold()

    @classmethod
    def _rate_limit_message(cls, response: requests.Response) -> bool:
        message = cls._error_message(response)
        return "rate limit" in message or "abuse detection" in message

    def _retry_reason(self, response: requests.Response) -> str:
        status = f"HTTP {response.status_code}"
        if response.status_code not in (403, 429):
            return status
        message = self._error_message(response)
        if "secondary rate limit" in message or "abuse detection" in message:
            return f"{status}, GitHub secondary rate limit"
        if response.headers.get("X-RateLimit-Remaining") == "0":
            return f"{status}, GitHub rate limit exhausted"
        if response.headers.get("Retry-After") is not None:
            return f"{status}, GitHub secondary rate limit"
        if "rate limit" in message:
            return f"{status}, GitHub rate limit"
        return status

    def _retry_delay(self, response: requests.Response, attempt: int) -> float | None:
        if response.status_code not in (403, 429):
            return super()._retry_delay(response, attempt)

        retry_after = self._parse_retry_after(response.headers.get("Retry-After"))
        if retry_after is not None:
            return retry_after

        if response.headers.get("X-RateLimit-Remaining") == "0":
            try:
                reset_at = float(response.headers["X-RateLimit-Reset"])
            except (KeyError, TypeError, ValueError):
                return self._backoff_delay(attempt)
            return max(reset_at - time.time(), self._backoff_delay(attempt))

        if self._rate_limit_message(response):
            return max(60.0, self._backoff_delay(attempt))
        if response.status_code == 429:
            return self._backoff_delay(attempt)
        return None

    def get_repository(self, owner: str, name: str) -> dict:
        return self.get_json(f"{self.API_URL}/repos/{owner}/{name}")

    def get_readme(self, owner: str, name: str, *, allow_blob: bool = False) -> Readme:
        """Read the Contents response. Only corpus backfill requests the optional blob."""
        root = f"{self.API_URL}/repos/{owner}/{name}"
        try:
            payload = self.get_json(f"{root}/readme")
        except HttpRequestError as exc:
            if exc.status_code == 404:
                return Readme(status="missing")
            raise
        if not isinstance(payload, dict):
            return Readme(status="invalid")
        path = payload.get("path") or payload.get("name") or ""
        sha = payload.get("sha") or ""
        if not isinstance(path, str) or not isinstance(sha, str):
            return Readme(status="invalid")
        result = _decode_readme(payload, path, sha)
        size = payload.get("size")
        if (allow_blob and result.status == "unavailable" and isinstance(size, int)
                and 0 < size <= MAX_README_BYTES and re.fullmatch(r"[0-9a-f]{40}", sha)):
            blob = self.get_json(f"{root}/git/blobs/{sha}")
            if not isinstance(blob, dict) or blob.get("sha") != sha or blob.get("size") != size:
                return Readme(status="invalid", path=path, sha=sha)
            return _decode_readme(blob, path, sha)
        return result

    def has_readme(self, owner: str, name: str, *, on_result: Callable[[Readme], None] | None = None) -> bool:
        result = self.get_readme(owner, name)
        if on_result is not None:
            try:
                on_result(result)
            except OSError as exc:
                logger.warning("Could not cache README for %s/%s: %s", owner, name, exc)
        return result.status != "missing"

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

    def user_repositories(self, login: str, limit: int) -> list[dict]:
        """Repositories the account owns, most recently pushed first.

        Forks are dropped: they carry the upstream's contributors, not this
        account's, and following them would harvest strangers. Sorting by
        update time means a truncated list keeps the live work.
        """
        owned: list[dict] = []
        for repository in self._paged(f"{self.API_URL}/users/{login}/repos",
                                      pages=(limit // 100) + 1,
                                      type="owner", sort="updated"):
            if repository.get("fork"):
                continue
            owned.append(repository)
            if len(owned) >= limit:
                break
        return owned
