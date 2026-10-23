from __future__ import annotations

import json
import time
from collections.abc import Iterable, Iterator

import requests

from .base import HttpClient, HttpRequestError


class GitHubClient(HttpClient):
    API_URL = "https://api.github.com"

    # Profiles per GraphQL query. A query costs one point of a separate
    # 5,000-an-hour budget whatever its size, where REST spends one request
    # of the core 5,000 per profile; profiles were 80% of the requests on
    # Innopolis.
    USERS_PER_QUERY = 50

    def __init__(self, timeout: int, token: str = "") -> None:
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        super().__init__(timeout, headers)
        self.has_token = bool(token)
        # (owner, name) lowercased -> REST-shaped repository payload with
        # "_has_readme", or None when GitHub knows no such repository.
        self._repositories: dict[tuple[str, str], dict | None] = {}

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

    REPOSITORIES_PER_QUERY = 25
    README_NAMES = ("README.md", "README.rst", "README.txt", "README", "readme.md", "Readme.md",
                    "README.markdown", "readme.rst", "README.MD")

    def prefetch_repositories(self, pairs: Iterable[tuple[str, str]]) -> None:
        """Load many repositories' metadata and README status at once.

        REST spends two requests a repository (the metadata, then /readme);
        a GraphQL query covers 25 for one point of its own budget. Afterwards
        get_repository and has_readme answer from what was loaded. Without a
        token (GraphQL needs one) this does nothing and both stay on REST.
        """
        if not self.has_token:
            return
        pairs = list(dict.fromkeys((owner.lower(), name.lower()) for owner, name in pairs))
        pairs = [pair for pair in pairs if pair not in self._repositories]
        readme = " ".join(f'f{i}: object(expression: {json.dumps("HEAD:" + n)}) {{ id }}'
                          for i, n in enumerate(self.README_NAMES))
        for start in range(0, len(pairs), self.REPOSITORIES_PER_QUERY):
            chunk = pairs[start:start + self.REPOSITORIES_PER_QUERY]
            fields = " ".join(
                f"r{i}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{"
                " databaseId url name description stargazerCount forkCount isArchived isFork pushedAt"
                " primaryLanguage { name } licenseInfo { spdxId } repositoryTopics(first: 20) { nodes { topic { name } } }"
                f" owner {{ __typename login url }} {readme} }}"
                for i, (owner, name) in enumerate(chunk)
            )
            try:
                data = self.request_json("POST", f"{self.API_URL}/graphql", json={"query": f"query {{ {fields} }}"})
            except HttpRequestError:
                # REST stays the fallback for this chunk.
                continue
            nodes = data.get("data") or {}
            for i, pair in enumerate(chunk):
                node = nodes.get(f"r{i}")
                self._repositories[pair] = None if node is None else {
                    "id": node.get("databaseId"),
                    "html_url": node.get("url"),
                    "name": node.get("name"),
                    "description": node.get("description"),
                    "stargazers_count": node.get("stargazerCount"),
                    "forks_count": node.get("forkCount"),
                    "archived": node.get("isArchived"),
                    "fork": node.get("isFork"),
                    "pushed_at": node.get("pushedAt"),
                    "language": (node.get("primaryLanguage") or {}).get("name"),
                    "license": {"spdx_id": (node.get("licenseInfo") or {}).get("spdxId")},
                    # Sorted as REST serves them.
                    "topics": sorted(n["topic"]["name"] for n in (node.get("repositoryTopics") or {}).get("nodes") or []),
                    "owner": {"login": (node.get("owner") or {}).get("login"),
                              "type": (node.get("owner") or {}).get("__typename"),
                              "html_url": (node.get("owner") or {}).get("url")},
                    "_has_readme": any(node.get(f"f{j}") for j in range(len(self.README_NAMES))),
                }

    def get_repository(self, owner: str, name: str) -> dict:
        key = (owner.lower(), name.lower())
        if key in self._repositories:
            payload = self._repositories[key]
            if payload is None:
                raise HttpRequestError("GET", f"{self.API_URL}/repos/{owner}/{name}", status_code=404)
            return payload
        return self.get_json(f"{self.API_URL}/repos/{owner}/{name}")

    def has_readme(self, owner: str, name: str) -> bool:
        """Check for a README via GET .../readme (200 = yes, 404 = no).

        Not exposed by the REST repository payload, so it needs its own call
        unless prefetch_repositories already looked.
        """
        payload = self._repositories.get((owner.lower(), name.lower()))
        if payload is not None:
            return payload["_has_readme"]
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

    def get_users(self, logins: Iterable[str]) -> dict[str, dict]:
        """Profiles of many accounts, shaped like get_user's; {} for an account that does not exist.

        GraphQL in batches when there is a token (GraphQL needs one), else
        one REST request each.
        """
        logins = list(dict.fromkeys(logins))
        if not self.has_token:
            found = {}
            for login in logins:
                try:
                    found[login] = self.get_user(login)
                except HttpRequestError as exc:
                    if exc.status_code != 404:
                        raise
                    found[login] = {}
            return found
        found = {}
        for start in range(0, len(logins), self.USERS_PER_QUERY):
            chunk = logins[start:start + self.USERS_PER_QUERY]
            fields = " ".join(
                f"u{i}: repositoryOwner(login: {json.dumps(login)}) {{ __typename login url"
                " ... on User { name email bio location company }"
                " ... on Organization { name email description location } }"
                for i, login in enumerate(chunk)
            )
            data = self.request_json("POST", f"{self.API_URL}/graphql", json={"query": f"query {{ {fields} }}"})
            nodes = data.get("data") or {}
            for i, login in enumerate(chunk):
                node = nodes.get(f"u{i}")
                found[login] = {
                    "login": node.get("login"),
                    "html_url": node.get("url"),
                    "type": node.get("__typename"),
                    "name": node.get("name"),
                    "email": node.get("email") or None,
                    "bio": node.get("bio") or node.get("description") or None,
                    "location": node.get("location"),
                    "company": node.get("company"),
                } if node else {}
        return found

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
