import unittest
from unittest import mock

import requests

from pauk.sources.base import HttpClient, HttpRequestError


class _Resp:
    def __init__(self, status: int, text: str = "", url: str = "https://example.org/page") -> None:
        self.status_code = status
        self.text = text
        self.content = text.encode()
        self.headers: dict[str, str] = {}
        self.url = url

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
        # 503 is retryable — the second attempt's body must be returned.
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


if __name__ == "__main__":
    unittest.main()
