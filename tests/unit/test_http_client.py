import threading
import time
import types
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
        self.kwargs = kwargs
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


class _Socket:
    def __init__(self) -> None:
        self.down = threading.Event()

    def shutdown(self, how: int) -> None:
        self.down.set()


class _Stalled(_Resp):
    """A body whose read blocks inside one chunk until the socket is shut down."""

    def __init__(self, chunks: int = 0, stall: bool = True) -> None:
        super().__init__(200)
        self.chunks, self.stall, self.closed = chunks, stall, False
        self.raw = types.SimpleNamespace(connection=types.SimpleNamespace(sock=_Socket()))

    def iter_content(self, size: int):
        yield from (b"x" for _ in range(self.chunks))
        if self.stall:
            # The real read sits in recv(); only the shutdown wakes it.
            if not self.raw.connection.sock.down.wait(5):
                raise AssertionError("the deadline never shut the socket down")
            raise requests.ConnectionError("connection reset")

    def close(self) -> None:
        self.closed = True


class GetBytesDeadlineTest(unittest.TestCase):
    def _client(self, response: _Resp) -> HttpClient:
        client = HttpClient(timeout=5)
        client.session = _FakeSession([response])
        return client

    def test_body_within_deadline_is_returned_streamed(self):
        client = self._client(_Stalled(3, stall=False))
        self.assertEqual(client.get_bytes("https://example.org/a.pdf", deadline=60), b"xxx")
        self.assertTrue(client.session.kwargs["stream"])

    def test_read_stalled_inside_a_chunk_is_cut_at_the_deadline(self):
        response = _Stalled(2)
        client = self._client(response)
        started = time.monotonic()
        with self.assertRaises(HttpRequestError) as ctx:
            client.get_bytes("https://example.org/a.pdf?token=SECRET", deadline=0.2)
        self.assertLess(time.monotonic() - started, 3)
        self.assertIn("not received in 0s", str(ctx.exception))
        self.assertNotIn("SECRET", str(ctx.exception))
        self.assertTrue(response.closed)

    def test_reader_socket_is_used_when_the_connection_let_go_of_it(self):
        # http.client hands the socket over to the response on Connection: close.
        response = _Stalled()
        sock = response.raw.connection.sock
        reader = types.SimpleNamespace(raw=types.SimpleNamespace(_sock=sock))
        response.raw = types.SimpleNamespace(connection=None, _fp=types.SimpleNamespace(fp=reader))
        client = self._client(response)
        with (mock.patch.object(_Stalled, "iter_content", lambda self, size: _wait_on(sock)),
              self.assertRaises(HttpRequestError)):
            client.get_bytes("https://example.org/a.pdf", deadline=0.2)
        self.assertTrue(sock.down.is_set())


def _wait_on(sock: _Socket):
    if not sock.down.wait(5):
        raise AssertionError("the deadline never shut the socket down")
    raise requests.ConnectionError("connection reset")
    yield b""


class MaxWaitTest(unittest.TestCase):
    @mock.patch("pauk.sources.base.time.sleep")
    def test_longer_retry_after_fails_without_waiting(self, sleep):
        client = HttpClient(timeout=5)
        client.session = _FakeSession([_Resp(503, headers={"Retry-After": "3600"})])
        with self.assertRaises(HttpRequestError) as ctx:
            client.get_bytes("https://example.org/a.pdf", max_wait=120)
        self.assertEqual(ctx.exception.status_code, 503)
        sleep.assert_not_called()

    @mock.patch("pauk.sources.base.time.sleep")
    def test_shorter_retry_after_is_still_honoured(self, sleep):
        client = HttpClient(timeout=5)
        client.session = _FakeSession([_Resp(503, headers={"Retry-After": "30"}), _Resp(200, text="pdf")])
        self.assertEqual(client.get_bytes("https://example.org/a.pdf", max_wait=120), b"pdf")
        sleep.assert_called_once_with(30.0)


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


if __name__ == "__main__":
    unittest.main()


class GitHubBatchUsersTest(unittest.TestCase):
    def test_profiles_come_in_one_graphql_query_shaped_like_rest(self):
        client = GitHubClient(5, token="t")
        reply = {"data": {
            "u0": {"__typename": "User", "login": "ana", "url": "https://github.com/ana", "name": "Ana Silva",
                   "email": "", "bio": "UFJF", "location": "Juiz de Fora", "company": None},
            "u1": {"__typename": "Organization", "login": "lab", "url": "https://github.com/lab", "name": "Lab",
                   "email": "lab@ufjf.br", "description": "A lab", "location": None},
            "u2": None,
        }}
        with mock.patch.object(client, "request_json", return_value=reply) as request:
            users = client.get_users(["ana", "lab", "ghost", "ana"])
        request.assert_called_once()
        self.assertEqual(users["ana"]["type"], "User")
        self.assertIsNone(users["ana"]["email"])
        self.assertEqual(users["lab"]["bio"], "A lab")
        self.assertEqual(users["lab"]["email"], "lab@ufjf.br")
        self.assertEqual(users["ghost"], {})

    def test_without_a_token_each_profile_is_one_rest_request(self):
        client = GitHubClient(5)
        with mock.patch.object(client, "get_user", side_effect=lambda login: {"login": login}) as get_user:
            self.assertEqual(client.get_users(["a", "b"]), {"a": {"login": "a"}, "b": {"login": "b"}})
        self.assertEqual(get_user.call_count, 2)
