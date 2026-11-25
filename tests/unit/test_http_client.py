import time
import unittest
from unittest import mock

import requests

from pauk.sources.base import HttpClient, HttpRequestError
from pauk.sources.github import GitHubClient
from pauk.sources.rate_limit import HostRateLimiter
from tests.unit.test_rate_limit import _Clock


class _Resp:
    def __init__(
        self, status: int, text: str = "", url: str = "https://example.org/page",
        headers: dict[str, str] | None = None, payload: dict | None = None,
    ) -> None:
        self.payload = payload
        self.status_code = status
        self.text = text
        self.content = text.encode()
        self.headers: dict[str, str] = headers or {}
        self.url = url

    def json(self) -> dict:
        if self.payload is None:
            raise ValueError("not json")
        return self.payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


class _FakeSession:
    """Stands in for requests.Session, replaying a queued sequence of responses."""

    def __init__(self, responses: list[_Resp]) -> None:
        self._responses = list(responses)
        self.calls = 0
        self.headers: dict[str, str] = {}
        self.queries: list[str] = []

    def request(self, method: str, url: str, **kwargs) -> _Resp:
        self.calls += 1
        if "query" in kwargs.get("json", {}):
            self.queries.append(kwargs["json"]["query"])
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class GetTextTest(unittest.TestCase):
    def _client(self, responses: list[_Resp]) -> HttpClient:
        client = HttpClient(timeout=5, headers={"User-Agent": "pauk-test"})
        client.session = _FakeSession(responses)
        return client

    def test_returns_decoded_body(self):
        client = self._client([_Resp(200, text="<html>ok</html>")])
        self.assertEqual(client.get_text("https://example.org/page"), "<html>ok</html>")

    @mock.patch("pauk.sources.base.time.sleep", lambda *_: None)
    def test_retries_transient_status(self):
        # 503 is retryable - the second attempt's body must be returned.
        client = self._client([_Resp(503), _Resp(200, text="recovered")])
        self.assertEqual(client.get_text("https://example.org/page"), "recovered")
        self.assertEqual(client.session.calls, 2)

    @mock.patch("pauk.sources.base.time.sleep", lambda *_: None)
    def test_retries_network_error_then_succeeds(self):
        # A transient network exception (timeout/connection) is retried, not raised.
        client = self._client([requests.ConnectionError("boom"), _Resp(200, text="ok")])
        self.assertEqual(client.get_text("https://example.org/page"), "ok")
        self.assertEqual(client.session.calls, 2)

    @mock.patch("pauk.sources.base.time.sleep", lambda *_: None)
    def test_network_error_exhausted_wraps_and_redacts(self):
        # When retries run out, the wrapped error redacts the URL built pre-request.
        url = "https://example.org/page?api_key=SECRET456"
        client = self._client([requests.ConnectionError("boom"), requests.ConnectionError("boom")])
        with self.assertRaises(HttpRequestError) as ctx:
            client.get_text(url, retries=1)
        message = str(ctx.exception)
        self.assertNotIn("SECRET456", message)
        self.assertIn("REDACTED", message)

    def test_error_redacts_url(self):
        # A token in the URL must never surface in the raised error message.
        url = "https://example.org/page?token=SECRET123"
        client = self._client([_Resp(404, url=url)])
        with self.assertRaises(HttpRequestError) as ctx:
            client.get_text(url)
        message = str(ctx.exception)
        self.assertNotIn("SECRET123", message)
        self.assertIn("REDACTED", message)  # url-encoded as %5BREDACTED%5D by redact_url


class WaitLogTest(unittest.TestCase):
    LOGGER = "pauk.sources.base"

    def _github(self, responses: list[_Resp]) -> GitHubClient:
        client = GitHubClient(timeout=5)
        client.session = _FakeSession(responses)
        client.rate_limiter = HostRateLimiter()
        self.enterContext(mock.patch.object(client.rate_limiter, "pause"))
        return client

    @mock.patch("pauk.sources.base.time.sleep")
    def test_exhausted_github_limit_logs_the_wait(self, sleep):
        reset = str(int(time.time()) + 3600)
        exhausted = _Resp(403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": reset})
        client = self._github([exhausted, _Resp(200, text="{}")])
        url = "https://api.github.com/repos/o/r?access_token=SECRET789"
        with self.assertLogs(self.LOGGER, level="WARNING") as logs:
            client.get_text(url)
        self.assertEqual(len(logs.output), 1)
        line = logs.output[0]
        self.assertRegex(line, r"waiting 3[56]\d\ds until \d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d before retrying GET ")
        self.assertIn("GitHub rate limit exhausted", line)
        self.assertIn("retry 1/3", line)
        self.assertNotIn("SECRET789", line)
        self.assertGreater(sleep.call_args.args[0], 3500)

    @mock.patch("pauk.sources.base.time.sleep")
    def test_secondary_github_limit_is_named(self, sleep):
        throttled = _Resp(403, headers={"Retry-After": "60"})
        client = self._github([throttled, _Resp(200, text="{}")])
        with self.assertLogs(self.LOGGER, level="WARNING") as logs:
            client.get_text("https://api.github.com/repos/o/r")
        self.assertIn("GitHub secondary rate limit", logs.output[0])
        self.assertIn("waiting 60s", logs.output[0])

    @mock.patch("pauk.sources.base.time.sleep")
    def test_secondary_limit_body_wins_over_exhausted_header(self, sleep):
        throttled = _Resp(
            429,
            headers={"X-RateLimit-Remaining": "0", "Retry-After": "60"},
            payload={"message": "You have exceeded a secondary rate limit."},
        )
        client = self._github([throttled, _Resp(200, text="{}")])
        with self.assertLogs(self.LOGGER, level="WARNING") as logs:
            client.get_text("https://api.github.com/repos/o/r")
        self.assertIn("HTTP 429, GitHub secondary rate limit", logs.output[0])

    @mock.patch("pauk.sources.base.time.sleep")
    def test_exhausted_limit_sent_as_429_waits_for_the_reset(self, sleep):
        reset = str(int(time.time()) + 3600)
        exhausted = _Resp(429, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": reset})
        client = self._github([exhausted, _Resp(200, text="{}")])
        with self.assertLogs(self.LOGGER, level="WARNING") as logs:
            client.get_text("https://api.github.com/repos/o/r")
        self.assertIn("HTTP 429, GitHub rate limit exhausted", logs.output[0])
        self.assertGreater(sleep.call_args.args[0], 3500)

    @mock.patch("pauk.sources.base.time.sleep")
    def test_plain_github_429_keeps_the_short_backoff(self, sleep):
        client = self._github([_Resp(429), _Resp(200, text="{}")])
        client.get_text("https://api.github.com/repos/o/r")
        sleep.assert_called_once_with(1.0)

    @mock.patch("pauk.sources.base.time.sleep")
    def test_secondary_limit_sent_as_429_without_headers_waits_a_minute(self, sleep):
        throttled = _Resp(429, payload={"message": "You have exceeded a secondary rate limit."})
        client = self._github([throttled, _Resp(200, text="{}")])
        with self.assertLogs(self.LOGGER, level="WARNING") as logs:
            client.get_text("https://api.github.com/repos/o/r")
        sleep.assert_called_once_with(60.0)
        self.assertIn("HTTP 429, GitHub secondary rate limit", logs.output[0])

    @mock.patch("pauk.sources.base.time.sleep")
    def test_reset_already_passed_keeps_the_backoff(self, sleep):
        past = str(int(time.time()) - 5)
        stale = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": past}
        client = self._github([_Resp(429, headers=stale), _Resp(429, headers=stale), _Resp(200, text="{}")])
        client.get_text("https://api.github.com/repos/o/r")
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.0, 2.0])

    @mock.patch("pauk.sources.base.time.sleep")
    def test_short_backoff_is_not_logged(self, sleep):
        client = HttpClient(timeout=5)
        client.session = _FakeSession([_Resp(503), _Resp(200, text="ok")])
        with self.assertNoLogs(self.LOGGER, level="WARNING"):
            self.assertEqual(client.get_text("https://example.org/page"), "ok")
        sleep.assert_called_once_with(1.0)


class GitHubBatchTest(unittest.TestCase):
    def setUp(self):
        self.client = GitHubClient(timeout=5, token="test")
        self.addCleanup(self.client.session.close)

    @staticmethod
    def repository():
        return {
            "fullDatabaseId": "3000000000", "name": "new", "url": "https://github.com/org/new",
            "owner": {"login": "org", "url": "https://github.com/org", "__typename": "Organization"},
            "stargazerCount": 0, "forkCount": 0, "isArchived": False, "isFork": False,
            "repositoryTopics": {"nodes": []}, "readme0": {"id": "blob"},
        }

    @staticmethod
    def user():
        return {"login": "Alice", "url": "https://github.com/Alice", "__typename": "User"}

    def test_graphql_primary_limit_retries_after_reset_without_pausing_rest(self):
        reset = str(int(time.time()) + 90)
        throttled = _Resp(200, url=self.client.GRAPHQL_URL, payload={"errors": [{"type": "RATE_LIMITED"}]},
                          headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": reset})
        ready = _Resp(200, url=self.client.GRAPHQL_URL, payload={"data": {"item0": self.user()}})
        self.client.session = _FakeSession([throttled, ready])
        self.client.rate_limiter = HostRateLimiter()
        with (mock.patch("pauk.sources.base.time.sleep") as sleep,
              mock.patch.object(self.client.rate_limiter, "pause") as pause):
            profiles = self.client.get_users(["alice"])
        self.assertEqual(profiles["alice"]["login"], "Alice")
        self.assertEqual(self.client.session.calls, 2)
        self.assertGreater(sleep.call_args.args[0], 88)
        pause.assert_not_called()

    def test_secondary_graphql_limit_pauses_rest_after_exhausted_retries_and_keeps_partial_data(self):
        clock = _Clock()
        limiter = HostRateLimiter(clock=clock)
        self.client.rate_limiter = limiter
        throttled = _Resp(200, url=self.client.GRAPHQL_URL, payload={
            "data": {"item0": self.user()},
            "errors": [{"type": "RATE_LIMITED", "message": "secondary rate limit"}],
        })
        failed = _Resp(200, url=self.client.GRAPHQL_URL, payload={
            "data": {"item1": None},
            "errors": [{"type": "RATE_LIMITED", "path": ["item2"]}],
        })
        self.client.session = _FakeSession([throttled, failed, failed, failed])

        def sleep(seconds):
            clock.now += seconds

        with mock.patch("pauk.sources.base.time.sleep", side_effect=sleep) as wait:
            result = self.client.get_users(["alice", "bob", "charlie"])
            later = self.client.get_users(["later"])
        self.assertEqual([call.args[0] for call in wait.call_args_list], [60., 120., 240.])
        self.assertEqual(self.client.session.calls, 4)
        self.assertIn("item0:", self.client.session.queries[0])
        self.assertTrue(all("item0:" not in query and "item1:" in query for query in self.client.session.queries[1:]))
        self.assertEqual(result["alice"]["login"], "Alice")
        self.assertEqual(result["bob"].status_code, 429)
        self.assertEqual(result["charlie"].status_code, 429)
        self.assertEqual(later["later"].status_code, 429)

        rest = GitHubClient(timeout=5)
        self.addCleanup(rest.session.close)
        rest.rate_limiter = limiter
        rest.session = _FakeSession([_Resp(200, payload={"login": "alice"})])
        state = limiter._hosts[self.client.API_HOST]
        with mock.patch.object(state.condition, "wait", side_effect=clock.wait) as wait:
            self.assertEqual(rest.get_user("alice")["login"], "alice")
        wait.assert_called_once_with(timeout=480.)

    def test_graphql_secondary_limit_uses_retry_after_for_http_200_and_403(self):
        for status in (200, 403):
            with self.subTest(status=status):
                response = _Resp(status, url=self.client.GRAPHQL_URL, headers={"Retry-After": "17"},
                                 payload={"errors": [{"type": "RATE_LIMITED"}]})
                self.assertEqual(self.client._retry_delay(response, 2), 17.)
        denied = _Resp(403, url=self.client.GRAPHQL_URL, payload={"message": "Resource not accessible"})
        self.assertIsNone(self.client._retry_delay(denied, 0))

    def test_repository_batches_deduplicate_keys_and_keep_canonical_metadata(self):
        data = {"item" + str(i): self.repository() for i in range(25)}
        replies = [{"data": data}, {"data": {"item0": self.repository()}}]
        pairs = [("org", f"repo{i}") for i in range(26)] + [("ORG", "REPO0")]
        with mock.patch.object(self.client, "request_json", side_effect=replies) as request:
            self.client.prefetch_repositories(pairs)
            repo = self.client.get_repository("Org", "Repo0")
            self.assertTrue(self.client.has_readme("ORG", "REPO25"))
            self.assertEqual(request.call_count, 2)
            queries = [call.kwargs["json"]["query"] for call in request.call_args_list]
            self.assertEqual([query.count("repository(owner:") for query in queries], [25, 1])
        self.assertEqual(repo["id"], 3_000_000_000)
        self.assertEqual(repo["html_url"], "https://github.com/org/new")

    def test_repository_errors_do_not_become_not_found_and_readme_misses_use_rest(self):
        repo = self.repository() | {"readme0": {}}
        graphql = {"data": {"item0": repo, "item1": None, "item2": None}, "errors": [
            {"path": ["item0", "readme0"], "type": "NOT_FOUND"},
            {"path": ["item1"], "type": "INTERNAL"},
            {"path": ["item2"], "type": "NOT_FOUND"},
        ]}
        with mock.patch.object(self.client, "request_json", side_effect=[
            graphql, {}, {"html_url": "https://github.com/org/renamed"},
        ]) as request:
            self.client.prefetch_repositories([("org", name) for name in ("good", "failed", "old")])
            self.assertTrue(self.client.has_readme("org", "good"))
            self.assertEqual(request.call_args.args[1], "https://api.github.com/repos/org/good/readme")
            with self.assertRaises(HttpRequestError) as error:
                self.client.get_repository("org", "failed")
            self.assertIsNone(error.exception.status_code)
            self.assertEqual(self.client.get_repository("org", "old")["html_url"], "https://github.com/org/renamed")
            self.assertEqual(request.call_count, 3)

    def test_profile_batches_normalize_case_and_keep_results_on_partial_failure(self):
        unavailable = {"path": ["item1", "name"], "type": "INTERNAL"}
        user = self.user() | {"userEmail": "alice@example.org"}
        users = {"data": {f"item{i}": user for i in range(50)}, "errors": [unavailable]}
        org = {
            "login": "lab", "url": "https://github.com/lab", "__typename": "Organization",
            "description": "Lab", "organizationEmail": "lab@example.org",
        }
        logins = [f"user{i}" for i in range(50)] + ["lab", "USER0"]
        with mock.patch.object(self.client, "request_json", side_effect=[users, {"data": {"item0": org}}]) as request:
            profiles = self.client.get_users(logins)
        self.assertEqual(len(profiles), 51)
        self.assertEqual(profiles["user0"]["login"], "Alice")
        self.assertEqual(profiles["user0"]["email"], "alice@example.org")
        self.assertIsInstance(profiles["user1"], HttpRequestError)
        self.assertEqual(profiles["lab"]["description"], "Lab")
        self.assertEqual(profiles["lab"]["type"], "Organization")
        self.assertEqual(profiles["lab"]["email"], "lab@example.org")
        queries = [call.kwargs["json"]["query"] for call in request.call_args_list]
        self.assertEqual([query.count("repositoryOwner(login:") for query in queries], [50, 1])
        self.assertIn("userEmail: email", queries[0])
        self.assertIn("organizationEmail: email", queries[0])

    def test_a_failed_repository_batch_does_not_skip_the_next_batch(self):
        failure = HttpRequestError("POST", self.client.GRAPHQL_URL, status_code=502)
        pairs = [("org", f"repo{i}") for i in range(self.client.REPOSITORIES_PER_QUERY + 1)]
        with mock.patch.object(self.client, "request_json", side_effect=[
            failure, {"data": {"item0": self.repository()}},
        ]) as request:
            self.client.prefetch_repositories(pairs)
            with self.assertRaises(HttpRequestError) as error:
                self.client.get_repository(*pairs[0])
            self.assertEqual(error.exception.status_code, 502)
            self.assertEqual(self.client.get_repository(*pairs[-1])["id"], 3_000_000_000)
        self.assertEqual(request.call_count, 2)

    def test_a_failed_profile_batch_does_not_skip_the_next_batch(self):
        failure = HttpRequestError("POST", self.client.GRAPHQL_URL, status_code=502)
        logins = [f"user{i}" for i in range(self.client.USERS_PER_QUERY + 1)]
        with mock.patch.object(self.client, "request_json", side_effect=[
            failure, {"data": {"item0": self.user()}},
        ]) as request:
            profiles = self.client.get_users(logins)
        self.assertEqual(profiles[logins[0]].status_code, 502)
        self.assertEqual(profiles[logins[-1]]["login"], "Alice")
        self.assertEqual(request.call_count, 2)

    def test_global_rate_limit_does_not_trigger_rest_or_a_second_batch(self):
        limit = {"data": None, "errors": [{"type": "RATE_LIMITED", "message": "private details"}]}
        with mock.patch.object(self.client, "request_json", return_value=limit) as request:
            profiles = self.client.get_users(f"user{i}" for i in range(51))
            later = self.client.get_users(["later"])
        request.assert_called_once()
        self.assertEqual(set(error.status_code for error in profiles.values()), {429})
        self.assertEqual(later["later"].status_code, 429)
        self.assertNotIn("private details", str(profiles["user0"]))

    def test_without_a_token_all_reads_use_rest_and_keep_errors_per_profile(self):
        client = GitHubClient(timeout=5)
        self.addCleanup(client.session.close)
        missing = HttpRequestError("GET", f"{client.API_URL}/users/gone", status_code=404)
        with mock.patch.object(client, "request_json", side_effect=[{"id": 1}, {}, missing, {"login": "here"}]) as request:
            client.prefetch_repositories([("org", "repo")])
            self.assertEqual(client.get_repository("org", "repo")["id"], 1)
            self.assertTrue(client.has_readme("org", "repo"))
            profiles = client.get_users(["gone", "here"])
        self.assertIs(profiles["gone"], missing)
        self.assertEqual(profiles["here"], {"login": "here"})
        self.assertEqual([call.args[0] for call in request.call_args_list], ["GET"] * 4)


if __name__ == "__main__":
    unittest.main()
