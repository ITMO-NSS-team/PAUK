from __future__ import annotations

import json
import time
from collections.abc import Iterable, Iterator

import requests

from .base import HttpClient, HttpRequestError


class GitHubClient(HttpClient):
    API_URL = "https://api.github.com"
    GRAPHQL_URL = f"{API_URL}/graphql"
    REPOSITORIES_PER_QUERY = 25
    USERS_PER_QUERY = 50
    REPOSITORY_TOPIC_LIMIT = 20
    README_NAMES = (
        "README.md", "README.rst", "README.txt", "README", "readme.md", "Readme.md",
        "README.markdown", "readme.rst", "README.MD",
    )

    def __init__(self, timeout: int, token: str = "") -> None:
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        super().__init__(timeout, headers)
        self._has_token = bool(token)
        self._graphql_blocked: HttpRequestError | None = None
        self._prefetched_repositories: dict[tuple[str, str], tuple[dict, bool] | HttpRequestError] = {}

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

    def _graphql_error(self, kind: str | None) -> HttpRequestError:
        status = {"FORBIDDEN": 403, "RATE_LIMITED": 429}.get(kind)
        return HttpRequestError("POST", self.GRAPHQL_URL, status_code=status, cause_type="GraphQL error")

    def _graphql_nodes(self, fields: Iterable[str]) -> list[dict | HttpRequestError | None]:
        """Return the result of each aliased field in request order."""
        if self._graphql_blocked is not None:
            raise self._graphql_blocked.with_traceback(None)
        selections = [f"item{i}: {field}" for i, field in enumerate(fields)]
        try:
            payload = self.request_json("POST", self.GRAPHQL_URL, json={"query": "query { " + " ".join(selections) + " }"})
        except HttpRequestError as error:
            if error.status_code in {401, 403, 429}:
                self._graphql_blocked = error
            raise
        except ValueError:
            raise HttpRequestError("POST", self.GRAPHQL_URL, cause_type="Invalid GraphQL JSON") from None
        if not isinstance(payload, dict):
            raise HttpRequestError("POST", self.GRAPHQL_URL, cause_type="Invalid GraphQL response")
        data = payload.get("data")
        errors = payload.get("errors") or []
        if (data is not None and not isinstance(data, dict)) or not isinstance(errors, list):
            raise HttpRequestError("POST", self.GRAPHQL_URL, cause_type="Invalid GraphQL response")
        data = data or {}
        field_errors: dict[str, HttpRequestError | None] = {}
        global_error = None
        for error in errors:
            if not isinstance(error, dict):
                raise HttpRequestError("POST", self.GRAPHQL_URL, cause_type="Invalid GraphQL error")
            extensions = error.get("extensions")
            kind = error.get("type") or (extensions.get("type") if isinstance(extensions, dict) else None)
            path = error.get("path")
            if isinstance(path, list) and path and isinstance(path[0], str):
                if kind == "NOT_FOUND" and len(path) == 1:
                    # Confirm missing objects over REST, which can also resolve old URLs.
                    field_errors.setdefault(path[0], None)
                elif kind != "NOT_FOUND" or len(path) != 2 or not str(path[1]).startswith("readme"):
                    field_errors[path[0]] = self._graphql_error(kind)
            else:
                failure = self._graphql_error(kind)
                if global_error is None or failure.status_code is not None:
                    global_error = failure
        if global_error is not None:
            if global_error.status_code in {403, 429}:
                self._graphql_blocked = global_error
            if not data:
                raise global_error
        results: list[dict | HttpRequestError | None] = []
        for i in range(len(selections)):
            alias = f"item{i}"
            node = data.get(alias)
            if alias in field_errors:
                results.append(field_errors[alias] or global_error)
            elif isinstance(node, dict) and node:
                results.append(node)
            elif global_error is not None:
                results.append(global_error)
            elif alias in data and node is None:
                results.append(None)
            else:
                results.append(HttpRequestError("POST", self.GRAPHQL_URL, cause_type="Missing GraphQL result"))
        return results

    def prefetch_repositories(self, pairs: Iterable[tuple[str, str]]) -> None:
        """Fetch metadata and common README paths for repositories in scope."""
        if not self._has_token:
            return
        unique_pairs = dict.fromkeys((owner.lower(), name.lower()) for owner, name in pairs)
        pending = [pair for pair in unique_pairs if pair not in self._prefetched_repositories]
        readme_fields = " ".join(
            f"readme{i}: object(expression: {json.dumps('HEAD:' + name)}) {{ ... on Blob {{ id }} }}"
            for i, name in enumerate(self.README_NAMES)
        )
        for start in range(0, len(pending), self.REPOSITORIES_PER_QUERY):
            chunk = pending[start:start + self.REPOSITORIES_PER_QUERY]
            fields = (
                f"repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{"
                " fullDatabaseId url name description stargazerCount forkCount isArchived isFork pushedAt"
                " primaryLanguage { name } licenseInfo { spdxId }"
                f" repositoryTopics(first: {self.REPOSITORY_TOPIC_LIMIT}) {{ nodes {{ topic {{ name }} }} }}"
                f" owner {{ __typename login url }} {readme_fields} }}"
                for owner, name in chunk
            )
            try:
                nodes = self._graphql_nodes(fields)
            except HttpRequestError as error:
                self._prefetched_repositories.update(dict.fromkeys(pending[start:], error))
                break
            for pair, node in zip(chunk, nodes, strict=True):
                if isinstance(node, HttpRequestError):
                    self._prefetched_repositories[pair] = node
                elif node is not None and node.get("fullDatabaseId") is not None:
                    try:
                        payload = self._repository_payload(node)
                    except (KeyError, TypeError, ValueError):
                        self._prefetched_repositories[pair] = HttpRequestError(
                            "POST", self.GRAPHQL_URL, cause_type="Invalid GraphQL repository",
                        )
                        continue
                    has_readme = any(file.get("id") for key, file in node.items()
                                     if key.startswith("readme") and isinstance(file, dict))
                    self._prefetched_repositories[pair] = (payload, has_readme)

    @staticmethod
    def _repository_payload(node: dict) -> dict:
        owner = node["owner"]
        license_info = node.get("licenseInfo")
        return {
            "id": int(node["fullDatabaseId"]),
            "html_url": node["url"],
            "name": node["name"],
            "description": node.get("description"),
            "stargazers_count": node["stargazerCount"],
            "forks_count": node["forkCount"],
            "archived": node["isArchived"],
            "fork": node["isFork"],
            "pushed_at": node.get("pushedAt"),
            "language": (node.get("primaryLanguage") or {}).get("name"),
            "license": {"spdx_id": license_info.get("spdxId")} if license_info else None,
            "topics": sorted(item["topic"]["name"] for item in node["repositoryTopics"]["nodes"] if item),
            "owner": {"login": owner["login"], "type": owner["__typename"], "html_url": owner["url"]},
        }

    def get_repository(self, owner: str, name: str) -> dict:
        prefetched = self._prefetched_repositories.get((owner.lower(), name.lower()))
        if isinstance(prefetched, HttpRequestError):
            raise prefetched.with_traceback(None)
        if prefetched is not None:
            return prefetched[0]
        return self.get_json(f"{self.API_URL}/repos/{owner}/{name}")

    def has_readme(self, owner: str, name: str) -> bool:
        """Use a prefetched file or ask GitHub for the preferred README."""
        prefetched = self._prefetched_repositories.get((owner.lower(), name.lower()))
        if isinstance(prefetched, tuple) and prefetched[1]:
            return True
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

    def get_users(self, logins: Iterable[str]) -> dict[str, dict | HttpRequestError]:
        """Fetch profiles keyed by lowercase login, with errors per login."""
        pending = list(dict.fromkeys(login.lower() for login in logins))
        found: dict[str, dict | HttpRequestError] = {}
        if not self._has_token:
            return {login: self._get_user_result(login) for login in pending}
        for start in range(0, len(pending), self.USERS_PER_QUERY):
            chunk = pending[start:start + self.USERS_PER_QUERY]
            fields = (
                f"repositoryOwner(login: {json.dumps(login)}) {{ __typename login url"
                " ... on User { name email bio location company }"
                " ... on Organization { name email description location } }"
                for login in chunk
            )
            try:
                nodes = self._graphql_nodes(fields)
            except HttpRequestError as error:
                found.update(dict.fromkeys(pending[start:], error))
                break
            for login, node in zip(chunk, nodes, strict=True):
                if node is None:
                    node = self._get_user_result(login)
                elif isinstance(node, dict) and node.get("login") and node.get("url") and node.get("__typename"):
                    node = {
                        "login": node["login"], "html_url": node["url"], "type": node["__typename"],
                        "name": node.get("name"), "email": node.get("email") or None,
                        "bio": node.get("bio"), "description": node.get("description"),
                        "location": node.get("location"), "company": node.get("company"),
                    }
                elif isinstance(node, dict):
                    node = HttpRequestError("POST", self.GRAPHQL_URL, cause_type="Invalid GraphQL profile")
                found[login] = node
        return found

    def _get_user_result(self, login: str) -> dict | HttpRequestError:
        try:
            user = self.get_user(login)
        except HttpRequestError as error:
            return error
        except ValueError:
            return HttpRequestError("GET", f"{self.API_URL}/users/{login}", cause_type="Invalid GitHub JSON")
        if isinstance(user, dict) and user.get("login"):
            return user
        return HttpRequestError("GET", f"{self.API_URL}/users/{login}", cause_type="Missing GitHub profile")

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
