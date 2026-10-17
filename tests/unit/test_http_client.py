import time
import unittest
from unittest import mock

import requests

from pauk.sources.base import HttpClient, HttpRequestError
from pauk.sources.github import GitHubClient


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

    def request(self, method: str, url: str, **kwargs) -> _Resp:
        self.calls += 1
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
    def test_short_backoff_is_not_logged(self, sleep):
        client = HttpClient(timeout=5)
        client.session = _FakeSession([_Resp(503), _Resp(200, text="ok")])
        with self.assertNoLogs(self.LOGGER, level="WARNING"):
            self.assertEqual(client.get_text("https://example.org/page"), "ok")
        sleep.assert_called_once_with(1.0)


if __name__ == "__main__":
    unittest.main()
