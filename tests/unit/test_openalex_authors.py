import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from pauk.sources.openalex import AUTHOR_BATCH_SIZE, OpenAlexClient, normalize_author_id
from pauk.sources.rate_limit import HostRateLimiter, RateLimit
from tests.unit.test_http_rate_limit import _attach
from tests.unit.test_rate_limit import _Clock


class OpenAlexAuthorsTest(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        limiter = HostRateLimiter(clock=self.clock)
        state = limiter._state("api.openalex.org", RateLimit(30))
        self.enterContext(mock.patch.object(state.condition, "wait", side_effect=self.clock.wait))
        self.enterContext(mock.patch("pauk.sources.base.RATE_LIMITER", limiter))
        self.requests = []
        self.payload = {"results": []}

        def handler(request):
            self.requests.append(request)
            return 200, {}, self.payload

        self.client = _attach(OpenAlexClient(5, "key"), handler)
        self.addCleanup(self.client.session.close)

    def test_normalizes_ids_and_maps_unordered_results_by_id(self):
        self.payload = {"results": [{"id": "https://openalex.org/A2"}, {"id": "https://openalex.org/A1"}]}
        result = self.client.get_authors(["a1", "https://openalex.org/A2/", "A1"])
        self.assertEqual(list(result), ["A1", "A2"])
        self.assertEqual(result["A1"]["id"], "https://openalex.org/A1")
        self.assertEqual(parse_qs(urlsplit(self.requests[0].url).query), {
            "filter": ["openalex_id:A1|A2"], "per_page": ["100"],
        })
        self.assertEqual(self.requests[0].headers["Authorization"], "Bearer key")

    def test_empty_batch_does_not_make_a_request(self):
        self.assertEqual(self.client.get_authors([]), {})
        self.assertEqual(self.requests, [])

    def test_batch_limit_and_invalid_ids_are_checked_before_the_request(self):
        with self.assertRaises(ValueError):
            self.client.get_authors([f"A{index}" for index in range(AUTHOR_BATCH_SIZE + 1)])
        for identifier in ("", "A1|A2", "A1?filter=x", "A1#fragment", None):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                normalize_author_id(identifier)
        self.assertEqual(self.requests, [])

    def test_missing_ids_stay_missing_and_unrequested_records_are_not_assigned(self):
        self.payload = {"results": [{"id": "https://openalex.org/A2"}, {"id": "https://openalex.org/A3"}]}
        self.assertEqual(set(self.client.get_authors(["A1", "A2"])), {"A2"})

    def test_merged_ids_follow_the_existing_alias_resolution(self):
        self.payload = {
            "meta": {"merged_ids": [
                {"id": "https://openalex.org/A1", "merge_into_id": "https://openalex.org/A2"},
                {"id": "https://openalex.org/A2", "merge_into_id": "https://openalex.org/A3"},
            ]},
            "results": [{"id": "https://openalex.org/A3", "display_name": "Survivor"}],
        }
        result = self.client.get_authors(["A1", "A2", "A3"])
        self.assertEqual(set(result), {"A1", "A2", "A3"})
        self.assertTrue(all(record["id"] == "https://openalex.org/A3" for record in result.values()))
        self.assertEqual(len(self.requests), 1)

    def test_alias_cycles_and_conflicting_survivors_leave_independent_results(self):
        for targets in ((('A1', 'A2'), ('A2', 'A1')), (('A1', 'A2'), ('A1', 'A3'))):
            with self.subTest(targets=targets):
                self.payload = {"results": [{"id": "A1"}, {"id": "A9"}], "meta": {"merged_ids": [
                    {"id": first, "merge_into_id": second} for first, second in targets
                ]}}
                self.assertEqual(self.client.get_authors(["A1", "A9"]), {"A9": {"id": "A9"}})

    def test_malformed_batch_response_is_not_treated_as_an_empty_success(self):
        for payload in ([], {}, {"results": None}, {"results": [], "meta": {"merged_ids": {}}}):
            with self.subTest(payload=payload):
                self.payload = payload
                with self.assertRaises(ValueError):
                    self.client.get_authors(["A1"])
