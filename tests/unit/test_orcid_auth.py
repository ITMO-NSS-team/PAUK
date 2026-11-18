import math
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock
from urllib.parse import parse_qs

from pauk.redaction import redact_text
from pauk.settings import Settings
from pauk.sources.base import HttpClient
from pauk.sources.orcid import TOKEN_URL, OrcidAuthenticationError, OrcidClient
from pauk.sources.rate_limit import HostRateLimiter, RateLimit
from tests.unit.test_http_rate_limit import _attach
from tests.unit.test_rate_limit import _Clock


class OrcidAuthenticationTest(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        limiter = HostRateLimiter(clock=self.clock)
        state = limiter._state("pub.orcid.org", RateLimit(12))
        self.enterContext(mock.patch.object(state.condition, "wait", side_effect=self.clock.wait))
        self.enterContext(mock.patch("pauk.sources.base.RATE_LIMITER", limiter))
        self.enterContext(mock.patch("pauk.sources.base.time.sleep", side_effect=self.clock.wait))
        self.enterContext(mock.patch("pauk.sources.orcid.monotonic", side_effect=self.clock))
        self.enterContext(mock.patch.dict(OrcidClient._tokens, clear=True))
        self.requests = []
        self.clients = []
        self.token_payload = {"access_token": "public-token", "token_type": "bearer", "expires_in": 1000}
        self.token_status = 200
        self.token_headers = {}
        self.record_status = 200
        self.handler = self.respond
        original_init = HttpClient.__init__

        def init(client, *args, **kwargs):
            original_init(client, *args, **kwargs)
            _attach(client, self.handler)
            client.session.close = mock.Mock(wraps=client.session.close)
            self.clients.append(client)
            self.addCleanup(client.session.close)

        self.enterContext(mock.patch.object(HttpClient, "__init__", init))

    def respond(self, request):
        self.requests.append(request)
        if request.url == TOKEN_URL:
            self.assertEqual(request.method, "POST")
            self.assertNotIn("Authorization", request.headers)
            self.assertEqual(request.headers["Content-Type"], "application/x-www-form-urlencoded")
            return self.token_status, self.token_headers, self.token_payload
        self.assertTrue(request.url.startswith("https://pub.orcid.org/v3.0/"))
        return self.record_status, {}, {"person": {}}

    def test_anonymous_reads_need_no_token(self):
        client = OrcidClient(5)
        self.assertEqual(client.get_record("0000-0001-2345-6789"), {"person": {}})
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn("Authorization", self.requests[0].headers)

    def test_registered_client_authorizes_only_when_a_record_is_requested(self):
        client = OrcidClient(5, "app", "a&secret=with+symbols")
        self.assertEqual(self.requests, [])
        client.get_record("0000-0001-2345-6789")
        client.get_record("0000-0001-2345-6790")
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(parse_qs(self.requests[0].body), {
            "client_id": ["app"], "client_secret": ["a&secret=with+symbols"],
            "grant_type": ["client_credentials"], "scope": ["/read-public"],
        })
        self.assertTrue(all(request.headers.get("Authorization") == "Bearer public-token"
                            for request in self.requests[1:]))
        self.assertTrue(all("client_secret" not in request.url for request in self.requests))
        token_client = next(client for client in self.clients if type(client) is HttpClient)
        token_client.session.close.assert_called_once_with()

    def test_threads_and_client_instances_share_one_token_request(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def handler(request):
            if request.url == TOKEN_URL:
                started.set()
                if not release.wait(2):
                    raise AssertionError("token request did not finish")
            return self.respond(request)

        self.handler = handler
        clients = [OrcidClient(5, "app", "secret") for _ in range(8)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(client.get_record, str(index)) for index, client in enumerate(clients)]
            try:
                self.assertTrue(started.wait(1))
            finally:
                release.set()
            self.assertTrue(all(future.result(timeout=2) == {"person": {}} for future in futures))
        self.assertEqual(sum(request.url == TOKEN_URL for request in self.requests), 1)

    def test_cache_is_bound_to_both_credentials(self):
        for identifier, secret in (("app", "one"), ("other", "one"), ("app", "two")):
            OrcidClient(5, identifier, secret).get_record("record")
        self.assertEqual(sum(request.url == TOKEN_URL for request in self.requests), 3)

    def test_expired_token_is_replaced_without_sending_it_to_the_token_endpoint(self):
        self.token_payload["expires_in"] = 10
        client = OrcidClient(5, "app", "secret")
        client.get_record("one")
        self.clock.now += 11
        self.token_payload["access_token"] = "new-token"
        client.get_record("two")
        self.assertEqual(sum(request.url == TOKEN_URL for request in self.requests), 2)
        self.assertEqual(self.requests[-1].headers["Authorization"], "Bearer new-token")

    def test_partial_credentials_are_rejected(self):
        for identifier, secret in (("app", ""), ("", "secret")):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                OrcidClient(5, identifier, secret)
        self.assertEqual(self.requests, [])

    def test_token_errors_are_safe_and_do_not_fall_back_to_anonymous_reads(self):
        for status in (400, 401, 403):
            with self.subTest(status=status):
                self.requests.clear()
                self.token_status = status
                self.token_payload = {"error_description": "credential-secret public-token"}
                with self.assertRaises(OrcidAuthenticationError) as caught:
                    OrcidClient(5, "app", "credential-secret").get_record("record")
                self.assertNotIn("credential-secret", str(caught.exception))
                self.assertNotIn("public-token", str(caught.exception))
                self.assertEqual([request.url for request in self.requests], [TOKEN_URL])

    def test_invalid_token_payload_is_not_cached(self):
        invalid = [[], {}, {"access_token": "bad\r\nheader"}, {"expires_in": True},
                   {"expires_in": 0}, {"expires_in": math.inf}, {"expires_in": 10**400}, {"token_type": "basic"}]
        for index, values in enumerate(invalid):
            with self.subTest(values=values):
                self.token_payload = ({"access_token": "public-token", "token_type": "bearer", "expires_in": 1000}
                                      | values) if isinstance(values, dict) else values
                if values == {}:
                    self.token_payload = {}
                client = OrcidClient(5, str(index), "secret")
                with self.assertRaises(OrcidAuthenticationError):
                    client.get_record("record")
                self.token_payload = {"access_token": "valid-token", "token_type": "bearer", "expires_in": 1000}
                self.assertEqual(client.get_record("record"), {"person": {}})

    def test_token_redirects_do_not_forward_credentials(self):
        for status in (302, 307, 308):
            with self.subTest(status=status):
                self.requests.clear()
                self.token_status = status
                self.token_headers = {"Location": "https://outside.test/token"}
                with self.assertRaises(OrcidAuthenticationError):
                    OrcidClient(5, "app", "secret").get_record("record")
                self.assertEqual([request.url for request in self.requests], [TOKEN_URL])

    def test_transient_token_failure_uses_the_existing_retry_loop(self):
        calls = []

        def handler(request):
            calls.append(request.url)
            if len(calls) == 1:
                self.requests.append(request)
                return 503, {"Retry-After": "2"}, {}
            return self.respond(request)

        self.handler = handler
        OrcidClient(5, "app", "secret").get_record("record")
        self.assertEqual(calls[:2], [TOKEN_URL, TOKEN_URL])

    def test_rejected_bearer_token_is_an_authentication_error(self):
        self.record_status = 401
        with self.assertRaises(OrcidAuthenticationError):
            OrcidClient(5, "app", "secret").get_record("record")
        self.assertEqual(len(self.requests), 2)

    def test_rejected_token_is_replaced_on_the_next_run(self):
        client = OrcidClient(5, "app", "secret")
        self.record_status = 401
        with self.assertRaises(OrcidAuthenticationError):
            client.get_record("record")
        self.record_status = 200
        self.token_payload["access_token"] = "replacement"
        client.get_record("record")
        self.assertEqual(sum(request.url == TOKEN_URL for request in self.requests), 2)
        self.assertEqual(self.requests[-1].headers["Authorization"], "Bearer replacement")

    def test_record_identifier_cannot_change_the_path_or_query(self):
        OrcidClient(5).get_record("record/part?extra=1#fragment")
        self.assertEqual(self.requests[0].url,
                         "https://pub.orcid.org/v3.0/record%2Fpart%3Fextra%3D1%23fragment/record")

    def test_configured_secret_is_not_shown_in_settings_or_diagnostics(self):
        value = "private-orcid-credential"
        self.assertNotIn(value, repr(Settings(orcid_client_secret=value)))
        with mock.patch.dict(os.environ, {"ORCID_CLIENT_SECRET": value}):
            self.assertNotIn(value, redact_text("authorization failed: " + value))
