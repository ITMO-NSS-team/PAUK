from __future__ import annotations

from .base import HttpClient


class GitHubClient(HttpClient):
    API_URL = "https://api.github.com"

    def __init__(self, timeout: int, token: str = "") -> None:
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        super().__init__(timeout, headers)

    def get_repository(self, owner: str, name: str) -> dict:
        return self.get_json(f"{self.API_URL}/repos/{owner}/{name}")

