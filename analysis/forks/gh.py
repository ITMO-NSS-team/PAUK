"""Minimal GitHub API helper with a disk cache and rate-limit waits."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

API = "https://api.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
CACHE = Path("/analysis/forks/cache")
CACHE.mkdir(parents=True, exist_ok=True)

session = requests.Session()
session.headers.update({
    "Accept": "application/vnd.github+json",
    "User-Agent": "pauk-fork-analysis",
})
if TOKEN:
    session.headers["Authorization"] = f"Bearer {TOKEN}"


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


def _wait_for_quota(response: requests.Response) -> None:
    if response.headers.get("X-RateLimit-Remaining") == "0":
        reset = float(response.headers.get("X-RateLimit-Reset", "0"))
        delay = max(reset - time.time(), 0) + 5
        print(f"rate limit exhausted, sleeping {delay:.0f}s", flush=True)
        time.sleep(delay)


def get(url: str, params: dict | None = None, attempts: int = 4) -> tuple[int, object]:
    """GET with retries; returns (status, payload) and never raises on 404/403."""
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(url, params=params, timeout=40)
        except requests.RequestException as exc:
            last = exc
            time.sleep(min(5 * attempt, 30))
            continue
        if response.status_code in (200, 201):
            return response.status_code, response.json()
        if response.status_code in (404, 410, 451):
            return response.status_code, None
        if response.status_code in (403, 429):
            _wait_for_quota(response)
            retry_after = response.headers.get("Retry-After")
            time.sleep(float(retry_after) if retry_after else min(10 * attempt, 60))
            last = ApiError(response.status_code, response.text[:200])
            continue
        if response.status_code >= 500:
            time.sleep(min(10 * attempt, 60))
            last = ApiError(response.status_code, response.text[:200])
            continue
        return response.status_code, None
    if isinstance(last, ApiError):
        raise last
    if last is not None:
        raise ApiError(0, str(last))
    return 0, None


def cached(key: str, fetch):
    """Fetch once, keep the payload on disk, and reuse it on later runs."""
    path = CACHE / f"{key}.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    payload = fetch()
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload


def paged(url: str, params: dict | None = None, limit: int = 10) -> tuple[list[dict], int]:
    """All pages of a list endpoint; returns (items, pages_read)."""
    items: list[dict] = []
    pages = 0
    page = 1
    while page <= limit:
        status, payload = get(url, params={"per_page": 100, "page": page, **(params or {})})
        pages += 1
        if status != 200 or not isinstance(payload, list):
            break
        items.extend(payload)
        if len(payload) < 100:
            break
        page += 1
    return items, pages
