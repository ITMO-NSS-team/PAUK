import io
import json
import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest import mock

import requests
from requests.adapters import BaseAdapter

from pauk.sources.base import HttpClient, HttpRequestError
from pauk.sources.crossref import CrossrefClient
from pauk.sources.openalex import OpenAlexClient
from pauk.sources.orcid import OrcidClient
from pauk.sources.rate_limit import HostRateLimiter, RateLimit
from tests.unit.test_rate_limit import _Clock

_SESSION_SEND = requests.Session.send


class _Adapter(BaseAdapter):
    def __init__(self, handler):
        self.handler = handler

    def send(self, request, **kwargs):
        status, headers, payload = self.handler(request)
        response = requests.Response()
        response.status_code = status
        response.headers.update(headers)
        response.url = request.url
        response.request = request
        response._content = json.dumps(payload).encode()
        response.raw = io.BytesIO(response._content)
        return response

    def close(self):
        pass


class _TransportSession(requests.Session):
    def send(self, request, **kwargs):
        # The benchmark suite patches Session.send; only our in-memory adapter may bypass that patch.
        if not isinstance(self.get_adapter(request.url), _Adapter):
            raise AssertionError("test attempted to use a real HTTP adapter")
        return _SESSION_SEND(self, request, **kwargs)


def _attach(client, handler):
    session = _TransportSession()
    session.headers.update(client.session.headers)
    client.session.close()
    client.session = session
    session.mount("https://", _Adapter(handler))
    return client


class HttpRateLimitTest(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.limiter = HostRateLimiter(clock=self.clock)
        self.enterContext(mock.patch("pauk.sources.base.RATE_LIMITER", self.limiter))
        self.sleep = self.enterContext(mock.patch("pauk.sources.base.time.sleep", side_effect=self.clock.wait))
        for host, limit in {
            "api.openalex.org": RateLimit(30),
            "pub.orcid.org": RateLimit(12),
            "api.crossref.org": RateLimit(10, 3),
        }.items():
            state = self.limiter._state(host, limit)
            self.enterContext(mock.patch.object(state.condition, "wait", side_effect=self.clock.wait))

    def attach(self, client, handler):
        _attach(client, handler)
        self.addCleanup(client.session.close)
        return client

    def test_two_clients_share_admission_times(self):
        starts = []

        def handler(_request):
            starts.append(self.clock())
            return 200, {}, {"id": "A1"}

        first = self.attach(OpenAlexClient(5, "key"), handler)
        second = self.attach(OpenAlexClient(5, "key"), handler)
        first.get_author("A1")
        second.get_author("A1")
        self.assertAlmostEqual(starts[1] - starts[0], 1 / 30)

    def test_anonymous_openalex_uses_the_public_ceiling(self):
        starts = []

        def handler(_request):
            starts.append(self.clock())
            return 200, {}, {}

        client = self.attach(OpenAlexClient(5), handler)
        client.get_author("A1")
        client.get_author("A2")
        self.assertAlmostEqual(starts[1] - starts[0], 0.1)

    def test_orcid_profile_uses_configured_rate(self):
        starts = []

        def handler(_request):
            starts.append(self.clock())
            return 200, {}, {}

        client = self.attach(OrcidClient(5, requests_per_second=6), handler)
        client.get_record("one")
        client.get_record("two")
        self.assertAlmostEqual(starts[1] - starts[0], 1 / 6)

    def test_crossref_contact_is_sent_and_public_fallback_is_conservative(self):
        seen = []

        def handler(request):
            seen.append((self.clock(), request.url))
            return 200, {}, {}

        client = self.attach(CrossrefClient(5, "contact@example.org"), handler)
        client.get_work("10.1234/a?x=1")
        self.assertIn("mailto=contact%40example.org", seen[0][1])
        self.assertIn("a%3Fx%3D1", seen[0][1])
        public = self.attach(CrossrefClient(5), handler)
        public.get_work("10.1234/b")
        public.get_work("10.1234/c")
        self.assertAlmostEqual(seen[2][0] - seen[1][0], 0.2)
        self.assertNotIn("mailto", seen[1][1])

    def test_server_headers_can_lower_but_not_raise_rate(self):
        starts = []

        def handler(_request):
            starts.append(self.clock())
            rate = "5" if len(starts) == 1 else "20"
            return 200, {"x-rate-limit-limit": rate, "x-rate-limit-interval": "1s", "x-concurrency-limit": "1"}, {}

        client = self.attach(CrossrefClient(5, "contact@example.org"), handler)
        for _ in range(3):
            client.get_work("10.1234/a")
        self.assertAlmostEqual(starts[1] - starts[0], 0.2)
        self.assertAlmostEqual(starts[2] - starts[1], 0.2)

    def test_openalex_daily_budget_does_not_change_request_rate(self):
        starts = []

        def handler(_request):
            starts.append(self.clock())
            return 200, {"X-RateLimit-Limit": "1"}, {}

        client = self.attach(OpenAlexClient(5, "key"), handler)
        client.get_author("A1")
        client.get_author("A2")
        self.assertAlmostEqual(starts[1] - starts[0], 1 / 30)

    def test_server_rate_interval_is_interpreted_in_seconds(self):
        starts = []

        def handler(_request):
            starts.append(self.clock())
            return 200, {"X-Rate-Limit-Limit": "6", "X-Rate-Limit-Interval": "2s"}, {}

        client = self.attach(CrossrefClient(5, "contact@example.org"), handler)
        client.get_work("10.1234/a")
        client.get_work("10.1234/b")
        self.assertAlmostEqual(starts[1] - starts[0], 1 / 3)

    def test_malformed_or_overflowing_server_headers_do_not_break_requests(self):
        values = (
            {"X-Rate-Limit-Limit": "nan", "X-Rate-Limit-Interval": "1s"},
            {"X-Rate-Limit-Limit": "5", "X-Rate-Limit-Interval": "0s"},
            {"X-Rate-Limit-Limit": "1e308", "X-Rate-Limit-Interval": "0." + "0" * 307 + "1s"},
            {"X-Concurrency-Limit": "-1"},
        )
        for headers in values:
            with self.subTest(headers=headers):
                client = self.attach(CrossrefClient(5, "contact@example.org"), lambda _, h=headers: (200, h, {}))
                self.assertEqual(client.get_work("10.1234/a"), {})

    def test_exhausted_retry_still_pauses_other_clients(self):
        first = self.attach(OpenAlexClient(5, "key"), lambda _: (429, {"Retry-After": "3"}, {}))
        with self.assertRaises(HttpRequestError):
            first.get_json("https://api.openalex.org/authors/A1", retries=0)
        self.sleep.assert_not_called()
        before = self.clock()
        starts = []

        def handler(_request):
            starts.append(self.clock())
            return 200, {}, {}

        second = self.attach(OpenAlexClient(5, "key"), handler)
        second.get_author("A2")
        self.assertAlmostEqual(starts[0] - before, 3)

    def test_retry_passes_through_the_same_limit_without_double_sleep(self):
        starts = []

        def handler(_request):
            starts.append(self.clock())
            return (503, {"Retry-After": "2"}, {}) if len(starts) == 1 else (200, {}, {})

        client = self.attach(OrcidClient(5), handler)
        client.get_record("one")
        self.assertEqual(len(starts), 2)
        self.assertAlmostEqual(starts[1] - starts[0], 2)
        self.sleep.assert_called_once_with(2)

    def test_http_date_retry_after_uses_existing_parser(self):
        now = datetime(2026, 11, 2, tzinfo=UTC)
        headers = {"Retry-After": format_datetime(now + timedelta(seconds=4), usegmt=True)}
        calls = []

        def handler(_request):
            calls.append(self.clock())
            return (429, headers, {}) if len(calls) == 1 else (200, {}, {})

        client = self.attach(OpenAlexClient(5, "key"), handler)
        with mock.patch("pauk.sources.base.datetime") as clock:
            clock.now.return_value = now
            client.get_author("A1")
        self.assertAlmostEqual(calls[1] - calls[0], 4)

    def test_network_failure_releases_the_slot_and_keeps_keys_redacted(self):
        starts = []

        def handler(_request):
            starts.append(self.clock())
            raise requests.ConnectionError("secret")

        client = self.attach(CrossrefClient(5), handler)
        with self.assertRaises(HttpRequestError) as caught:
            client.get_json("https://api.crossref.org/works/10.1234/a?api_key=secret", retries=0)
        self.assertNotIn("secret", str(caught.exception))
        client.session.mount("https://", _Adapter(lambda _: (200, {}, {})))
        client.get_work("10.1234/a")

    def test_each_redirect_has_its_own_admission(self):
        starts = []

        def handler(request):
            starts.append(self.clock())
            if request.url.endswith("/A1"):
                return 301, {"Location": "/authors/A2"}, {}
            return 200, {}, {"id": "A2"}

        client = self.attach(OpenAlexClient(5, "key"), handler)
        response = client._request("GET", "https://api.openalex.org/authors/A1", retries=0)
        self.assertEqual(response.json(), {"id": "A2"})
        self.assertEqual(len(response.history), 1)
        self.assertAlmostEqual(starts[1] - starts[0], 1 / 30)

    def test_redirect_preserves_environment_ca_bundle(self):
        for variable, trust_env in (("REQUESTS_CA_BUNDLE", True), ("CURL_CA_BUNDLE", True),
                                    ("REQUESTS_CA_BUNDLE", False)):
            with self.subTest(variable=variable, trust_env=trust_env):
                seen = []

                def handler(request, seen=seen):
                    seen.append(request)
                    return (301, {"Location": "/authors/A2"}, {}) if len(seen) == 1 else (200, {}, {})

                client = self.attach(OpenAlexClient(5, "key"), handler)
                client.session.trust_env = trust_env
                adapter = client.session.get_adapter(client.AUTHORS_URL)
                environment = {"REQUESTS_CA_BUNDLE": "", "CURL_CA_BUNDLE": ""}
                environment[variable] = "/test/ca.pem"
                with (
                    mock.patch.dict(os.environ, environment),
                    mock.patch.object(adapter, "send", wraps=adapter.send) as send,
                ):
                    client.get_author("A1")
                expected = "/test/ca.pem" if trust_env else True
                self.assertEqual([call.kwargs.get("verify") for call in send.call_args_list], [expected, expected])

    def test_openalex_redirect_keeps_authenticated_profile(self):
        seen = []

        def handler(request):
            seen.append(request)
            return (301, {"Location": "/authors/A2"}, {}) if len(seen) == 1 else (200, {}, {})

        client = self.attach(OpenAlexClient(5, "key"), handler)
        client.get_author("A1")
        self.assertEqual(seen[1].headers.get("Authorization"), "Bearer key")
        self.assertNotIn("api_key", seen[1].url)

    def test_crossref_redirect_keeps_contact_when_query_is_replaced(self):
        seen = []

        def handler(request):
            seen.append(request)
            return (301, {"Location": "/works/10.1234/new"}, {}) if len(seen) == 1 else (200, {}, {})

        client = self.attach(CrossrefClient(5, "contact@example.org"), handler)
        client.get_work("10.1234/old")
        self.assertIn("mailto:contact@example.org", seen[1].headers["User-Agent"])

    def test_requests_still_strips_auth_on_cross_host_redirect(self):
        seen = []

        def handler(request):
            seen.append(request)
            if len(seen) == 1:
                return 302, {"Location": "https://other.test/author"}, {}
            return 200, {}, {}

        client = self.attach(OpenAlexClient(5, "key"), handler)
        client.session.headers["Authorization"] = "Bearer secret"
        client.get_author("A1")
        self.assertNotIn("Authorization", seen[1].headers)
        self.assertNotIn("key", seen[1].url)

    def test_https_downgrade_strips_auth_and_uses_the_public_profile(self):
        seen = []

        def handler(request):
            seen.append((self.clock(), request))
            if len(seen) == 1:
                return 302, {"Location": "http://api.openalex.org/authors/A2"}, {}
            return 200, {}, {}

        client = self.attach(OpenAlexClient(5, "key"), handler)
        client.session.mount("http://", _Adapter(handler))
        client.get_author("A1")
        self.assertNotIn("Authorization", seen[1][1].headers)
        self.assertAlmostEqual(seen[1][0] - seen[0][0], 0.1)

    def test_return_from_another_origin_uses_the_public_profile(self):
        seen = []

        def handler(request):
            seen.append((self.clock(), request))
            if len(seen) == 1:
                return 302, {"Location": "https://other.test/author"}, {}
            if len(seen) == 2:
                return 302, {"Location": "https://api.openalex.org/authors/A2"}, {}
            return 200, {}, {}

        client = self.attach(OpenAlexClient(5, "key"), handler)
        client.get_author("A1")
        self.assertNotIn("Authorization", seen[2][1].headers)
        self.assertAlmostEqual(seen[2][0] - seen[0][0], 0.1)

    def test_cross_origin_redirect_respects_a_lower_configured_ceiling(self):
        starts = []

        def handler(request):
            if request.url.startswith("https://api.crossref.org/"):
                return 302, {"Location": "https://pub.orcid.org/record"}, {}
            starts.append(self.clock())
            return 200, {}, {}

        client = self.attach(HttpClient(5, rate_limits={"pub.orcid.org": RateLimit(2, 1)}), handler)
        for _ in range(2):
            client.get_json("https://api.crossref.org/start", retries=0)
        self.assertAlmostEqual(starts[1] - starts[0], 0.5)

    def test_requests_still_rewrites_post_after_302(self):
        seen = []

        def handler(request):
            seen.append(request)
            return (302, {"Location": "/next"}, {}) if len(seen) == 1 else (200, {}, {})

        client = self.attach(HttpClient(5), handler)
        client.request_json("POST", "https://api.openalex.org/start", json={"x": 1}, retries=0)
        self.assertEqual([request.method for request in seen], ["POST", "GET"])
        self.assertIsNone(seen[1].body)

    def test_redirect_limit_still_raises_a_redacted_error(self):
        client = self.attach(OpenAlexClient(5, "secret"), lambda _: (302, {"Location": "/authors/A1"}, {}))
        client.session.max_redirects = 2
        with self.assertRaises(HttpRequestError) as caught:
            client.get_json("https://api.openalex.org/authors/A1?api_key=secret", retries=0)
        self.assertNotIn("secret", str(caught.exception))
        self.assertIn("TooManyRedirects", str(caught.exception))


class HttpPauseConcurrencyTest(unittest.TestCase):
    def test_pause_is_set_before_another_waiting_client_is_released(self):
        limiter = HostRateLimiter()
        limit = {"limited.test": RateLimit(10000, 1)}
        first_sent = threading.Event()
        finish_first = threading.Event()
        times = {}

        def first_handler(_request):
            first_sent.set()
            if not finish_first.wait(timeout=2):
                raise AssertionError("first response was never released")
            times["response"] = time.monotonic()
            return 429, {"Retry-After": "0.05"}, {}

        def second_handler(_request):
            times["next"] = time.monotonic()
            return 200, {}, {}

        first = HttpClient(5, rate_limits=limit, rate_limiter=limiter)
        second = HttpClient(5, rate_limits=limit, rate_limiter=limiter)
        _attach(first, first_handler)
        _attach(second, second_handler)
        self.addCleanup(first.session.close)
        self.addCleanup(second.session.close)

        with ThreadPoolExecutor(max_workers=2) as pool:
            failing = pool.submit(first.get_json, "https://limited.test/first", retries=0)
            self.assertTrue(first_sent.wait(timeout=1))
            following = pool.submit(second.get_json, "https://limited.test/second", retries=0)
            finish_first.set()
            with self.assertRaises(HttpRequestError):
                failing.result(timeout=2)
            self.assertEqual(following.result(timeout=2), {})
        self.assertGreaterEqual(times["next"] - times["response"], 0.05)


class ClientLimitValidationTest(unittest.TestCase):
    def test_invalid_configuration_is_not_hidden_by_public_fallback(self):
        for factory in (OpenAlexClient, OrcidClient, CrossrefClient):
            with self.subTest(factory=factory), self.assertRaises(ValueError):
                factory(5, requests_per_second=float("inf"))
        with self.assertRaises(ValueError):
            CrossrefClient(5, max_concurrent=0)
        with self.assertRaises(ValueError):
            CrossrefClient(5, "not-an-email")
