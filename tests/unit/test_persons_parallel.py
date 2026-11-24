import tempfile
import threading
import time
import unittest
from concurrent.futures import CancelledError
from dataclasses import replace
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import mongomock

from pauk.models import Person
from pauk.models.processing import ProcessingStatus
from pauk.pipeline.stages.base import PreparedSelection
from pauk.pipeline.stages.dedup import DedupStage
from pauk.pipeline.stages.persons import PersonsStage
from pauk.settings import Settings
from pauk.sources.openalex import OpenAlexClient
from pauk.sources.orcid import OrcidClient
from pauk.sources.rate_limit import HostRateLimiter, RateLimit
from pauk.storage import PreparedStore, RawStore
from pauk.storage.person_identity import openalex_orcid_references
from tests.unit.test_http_rate_limit import _attach
from tests.unit.test_rate_limit import _Clock


class PersonsParallelTest(unittest.TestCase):
    ORCIDS = {"A1": "0000-0002-1825-0097", "A2": "0000-0001-5109-3700"}

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = Settings(data_dir=Path(temporary.name), persons_concurrency=2,
                               openalex_api_key="test-key", orcid_client_id="", orcid_client_secret="")
        self.db = mongomock.MongoClient()["persons_parallel"]
        self.prepared, self.raw = PreparedStore(self.db, "sample"), RawStore(self.db, "sample")
        self.requests = []
        self.clients = []
        self.lock = threading.Lock()
        self.handler = None
        self.omitted = set()
        self.with_orcid = False
        self.clock = _Clock()
        self.limiter = HostRateLimiter(clock=self.clock)
        self.enterContext(mock.patch("pauk.sources.base.RATE_LIMITER", self.limiter))
        for host, rate in (("api.openalex.org", 30), ("pub.orcid.org", 12)):
            state = self.limiter._state(host, RateLimit(rate))
            self.enterContext(mock.patch.object(state.condition, "wait", side_effect=self.clock.wait))

        def factory(kind):
            def create(*args, **kwargs):
                client = kind(*args, **kwargs)

                def handler(request):
                    with self.lock:
                        self.requests.append((request, threading.get_ident(), id(client.session)))
                    return self.handler(request, client) if self.handler else self.respond(request)

                _attach(client, handler)
                client.close = mock.Mock(wraps=client.close)
                with self.lock:
                    self.clients.append(client)
                return client
            return create

        self.enterContext(mock.patch("pauk.pipeline.stages.persons.OpenAlexClient", factory(OpenAlexClient)))
        self.enterContext(mock.patch("pauk.pipeline.stages.persons.OrcidClient", factory(OrcidClient)))
        self.enterContext(mock.patch("pauk.pipeline.stages.persons.CrossrefClient"))

    def author(self, identifier):
        result = {"id": f"https://openalex.org/{identifier}", "display_name": f"Author {identifier}"}
        if self.with_orcid:
            result["orcid"] = "https://orcid.org/" + self.ORCIDS[identifier]
        return result

    def respond(self, request):
        parsed = urlsplit(request.url)
        if parsed.hostname == "api.openalex.org":
            if parsed.path == "/authors":
                identifiers = parse_qs(parsed.query)["filter"][0].split(":", 1)[1].split("|")
                return 200, {}, {"results": [self.author(key) for key in reversed(identifiers) if key not in self.omitted]}
            return 200, {}, self.author(parsed.path.rsplit("/", 1)[-1])
        return 200, {}, {"person": {"emails": {"email": [{"email": "researcher@example.org"}]}}}

    def people(self, count):
        self.prepared.write_models("persons", [
            Person(id=f"P{number}", openalex_id=f"A{number}", is_itmo=False) for number in range(1, count + 1)
        ])

    def rows(self):
        return {person.id: person for person in self.prepared.read_models("persons", Person)}

    def test_author_batches_and_all_model_writes_run_on_the_coordinator(self):
        self.people(205)
        coordinator = threading.get_ident()
        save, append = self.prepared.upsert_models, self.raw.append

        def saved(*args, **kwargs):
            self.assertEqual(threading.get_ident(), coordinator)
            return save(*args, **kwargs)

        def appended(*args, **kwargs):
            self.assertEqual(threading.get_ident(), coordinator)
            return append(*args, **kwargs)

        with (mock.patch.object(self.prepared, "upsert_models", side_effect=saved),
              mock.patch.object(self.raw, "append", side_effect=appended)):
            PersonsStage(self.prepared, self.raw, self.config).run()
        self.assertEqual(len(self.requests), 3)
        self.assertTrue(all(person.name_raw == f"Author {person.openalex_id}" for person in self.rows().values()))
        self.assertTrue(all(client.close.call_count == 1 for client in self.clients))

    def test_workers_have_separate_sessions(self):
        self.people(250)
        barrier = threading.Barrier(3)

        def handler(request, _client):
            barrier.wait(timeout=2)
            return self.respond(request)

        self.handler = handler
        config = replace(self.config, persons_concurrency=3)
        PersonsStage(self.prepared, self.raw, config).run()
        self.assertTrue(all(person.processing["openalex_author"].status == ProcessingStatus.COMPLETED
                            for person in self.rows().values()))
        self.assertEqual(len(self.clients), 3)
        sessions = {}
        for _request, thread, session in self.requests:
            sessions.setdefault(thread, set()).add(session)
        self.assertEqual(len(sessions), 3)
        self.assertTrue(all(len(values) == 1 for values in sessions.values()))
        self.assertEqual(len({id(client.session) for client in self.clients}), 3)

    def test_a_slow_first_batch_does_not_block_the_next_window_slot(self):
        self.people(201)
        release = threading.Event()
        self.addCleanup(release.set)
        observed = []

        def handler(request, _client):
            if (parse_qs(urlsplit(request.url).query).get("filter", [""])[0].startswith("openalex_id:A1|A2|")
                    and not release.wait(2)):
                raise AssertionError("slow batch blocked the request window")
            return self.respond(request)

        def progress(label, _done, _total):
            if label == "Authors: OpenAlex" and not observed:
                rows = self.rows()
                if "openalex_author" in rows["P201"].processing:
                    self.assertNotIn("openalex_author", rows["P1"].processing)
                    observed.append(True)
                    release.set()

        self.handler = handler
        stage = PersonsStage(self.prepared, self.raw, self.config, on_progress=progress)
        stage.REPORT_SECONDS = 0
        stage.run()
        self.assertTrue(observed)

    def test_duplicate_ids_are_fetched_once_for_both_sources(self):
        self.with_orcid = True
        self.prepared.write_models("persons", [
            Person(id="P1", openalex_id="A1", is_itmo=False),
            Person(id="P2", openalex_id="A1", is_itmo=False),
        ])
        PersonsStage(self.prepared, self.raw, self.config).run()
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.db.raw.count_documents({"source": "openalex_authors"}), 1)
        self.assertEqual(self.db.raw.count_documents({"source": "orcid"}), 1)
        self.assertTrue(all(person.emails == ["researcher@example.org"] for person in self.rows().values()))

    def test_an_openalex_identifier_added_later_reopens_a_not_applicable_record(self):
        self.prepared.write_models("persons", [Person(id="per_test", is_itmo=False)])
        stage = PersonsStage(self.prepared, self.raw, self.config)
        stage.run()
        person = self.rows()["per_test"]
        self.assertEqual(person.processing["openalex_author"].status, ProcessingStatus.NOT_APPLICABLE)
        self.assertEqual(self.requests, [])

        person = Person.model_validate(person.model_dump() | {"identifiers": [
            {"scheme": "openalex", "value": "https://openalex.org/a123/"},
            {"scheme": "openalex", "value": "A123"},
            {"scheme": "dblp", "value": "A456"},
        ]})
        self.prepared.upsert_models("persons", [person])
        stage.run()
        stage.run()
        person = self.rows()["per_test"]
        self.assertIsNone(person.openalex_id)
        self.assertEqual(person.processing["openalex_author"].request_key, "A123")
        self.assertEqual(person.processing["openalex_author"].status, ProcessingStatus.COMPLETED)
        self.assertEqual(person.name_raw, "Author A123")
        self.assertEqual([urlsplit(request.url).path for request, *_ in self.requests], ["/authors/A123"])
        self.assertEqual(next(self.raw.read("openalex_authors"))["request"], {"author_id": "A123"})

    def test_ambiguous_openalex_identifiers_leave_only_that_author_failed(self):
        self.prepared.write_models("persons", [
            Person(id="per_ambiguous", is_itmo=False, identifiers=[
                {"scheme": "openalex", "value": "A1"}, {"scheme": "openalex", "value": "A2"},
            ]),
            Person(id="A3", is_itmo=False),
        ])
        stage = PersonsStage(self.prepared, self.raw, self.config)
        stage.run()
        state = self.rows()["per_ambiguous"].processing["openalex_author"]
        self.assertEqual(state.status, ProcessingStatus.FAILED)
        self.assertIn("Multiple OpenAlex author IDs", state.error)
        self.assertEqual(self.rows()["A3"].processing["openalex_author"].status, ProcessingStatus.COMPLETED)
        self.assertEqual([urlsplit(request.url).path for request, *_ in self.requests], ["/authors/A3"])

    def test_missing_batch_id_is_retrieved_individually(self):
        self.people(2)
        self.omitted = {"A2"}
        PersonsStage(self.prepared, self.raw, self.config).run()
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(self.requests[1][0].url.endswith("/authors/A2"))
        self.assertTrue(all(person.processing["openalex_author"].status == ProcessingStatus.COMPLETED
                            for person in self.rows().values()))

    def test_merged_author_without_orcid_overrides_prepared_orcid_in_dedup(self):
        self.people(2)
        people = self.rows()
        people["P1"].orcid = "0000-0002-1825-0097"
        self.prepared.upsert_models("persons", [people["P1"]])

        def handler(request, _client):
            if urlsplit(request.url).path == "/authors":
                return 200, {}, {"results": [self.author("A3"), self.author("A2")],
                                 "meta": {"merged_ids": [{"id": "A1", "merge_into_id": "A3"}]}}
            return self.respond(request)

        self.handler = handler
        PersonsStage(self.prepared, self.raw, self.config).run()
        refs = openalex_orcid_references(self.db)
        self.assertEqual(refs, {"A1": None, "A2": None, "A3": None})
        other = RawStore(self.db, "another group")
        other.append("openalex_authors", self.author("A3") | {"orcid": people["P1"].orcid}, {})
        self.assertEqual(openalex_orcid_references(self.db)["A1"], people["P1"].orcid)
        dedup = DedupStage(self.prepared, self.raw, self.config)
        self.assertIsNone(dedup._trusted_orcids(list(self.rows().values()))["P1"])

    def test_malformed_single_author_does_not_enter_raw_or_break_dedup(self):
        self.people(1)
        self.handler = lambda *_: (200, {}, ["not a profile"])
        stage = PersonsStage(self.prepared, self.raw, self.config)
        stage.run()
        self.assertEqual(self.rows()["P1"].processing["openalex_author"].status, ProcessingStatus.FAILED)
        self.assertEqual(list(self.raw.read("openalex_authors")), [])
        self.raw.append("openalex_authors", ["historical invalid record"], {"author_id": "A1"})
        self.assertEqual(openalex_orcid_references(self.db), {})
        self.handler = None
        stage.run()
        self.assertEqual(self.rows()["P1"].processing["openalex_author"].status, ProcessingStatus.COMPLETED)

    def test_single_author_only_accepts_a_changed_id_when_openalex_confirms_the_merge(self):
        self.people(1)
        stage = PersonsStage(self.prepared, self.raw, self.config)
        confirmed = False

        def merged(request, _client):
            if urlsplit(request.url).path == "/authors":
                metadata = {"merged_ids": [{"id": "A1", "merge_into_id": "A2"}]} if confirmed else {}
                return 200, {}, {"results": [self.author("A2")], "meta": metadata}
            return 200, {}, self.author("A2")

        self.handler = merged
        stage.run()
        person = self.rows()["P1"]
        self.assertEqual(person.processing["openalex_author"].status, ProcessingStatus.FAILED)
        self.assertIn("without a confirmed merge", person.processing["openalex_author"].error)
        self.assertIsNone(person.name_raw)
        self.assertEqual(list(self.raw.read("openalex_authors")), [])

        confirmed = True
        stage.run()
        self.assertEqual(self.rows()["P1"].processing["openalex_author"].status, ProcessingStatus.COMPLETED)
        self.assertEqual(openalex_orcid_references(self.db), {"A1": None, "A2": None})

    def test_foreign_orcid_url_does_not_trigger_an_orcid_lookup(self):
        self.people(1)
        foreign_url = f"https://example.org/{self.ORCIDS['A1']}"
        self.handler = lambda *_: (200, {}, self.author("A1") | {"orcid": foreign_url})
        PersonsStage(self.prepared, self.raw, self.config).run()
        person = self.rows()["P1"]
        self.assertIsNone(person.orcid)
        self.assertEqual(person.processing["openalex_author"].status, ProcessingStatus.COMPLETED)
        self.assertEqual(person.processing["orcid"].status, ProcessingStatus.NOT_APPLICABLE)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(openalex_orcid_references(self.db)["A1"], foreign_url)

    def test_invalid_orcid_response_preserves_employment_until_a_valid_empty_record(self):
        self.prepared.write_models("persons", [Person(
            id="P1", orcid="O1", is_itmo=False,
            affiliations=[{"name": "Employer", "source": "orcid", "source_person_id": "P1", "source_id": "O1"}],
        )])
        stage = PersonsStage(self.prepared, self.raw, self.config)
        for payload in ({}, {"error": "unavailable"}, {"activities-summary": []},
                        {"activities-summary": {"employments": {"affiliation-group": [{}]}}}):
            with self.subTest(payload=payload):
                self.handler = mock.Mock(return_value=(200, {}, payload))
                stage.run()
                person = self.rows()["P1"]
                self.assertEqual(person.processing["orcid"].status, ProcessingStatus.FAILED)
                self.assertEqual([row.name for row in person.affiliations], ["Employer"])
                self.assertEqual(list(self.raw.read("orcid")), [])
        self.handler = mock.Mock(return_value=(200, {}, {"person": {}}))
        stage.run()
        self.assertEqual([row.name for row in self.rows()["P1"].affiliations], ["Employer"])
        self.handler = mock.Mock(return_value=(200, {}, {"activities-summary": {"employments": {"affiliation-group": []}}}))
        PersonsStage(self.prepared, self.raw, self.config, force=True).run()
        person = self.rows()["P1"]
        self.assertEqual(person.processing["orcid"].status, ProcessingStatus.COMPLETED)
        self.assertEqual(person.affiliations, [])

    def test_a_deleted_id_does_not_fail_the_other_batch_members(self):
        self.people(2)
        self.omitted = {"A2"}

        def handler(request, _client):
            return (404, {}, {}) if request.url.endswith("/authors/A2") else self.respond(request)

        self.handler = handler
        PersonsStage(self.prepared, self.raw, self.config).run()
        rows = self.rows()
        self.assertEqual(rows["P1"].processing["openalex_author"].status, ProcessingStatus.COMPLETED)
        self.assertEqual(rows["P2"].processing["openalex_author"].status, ProcessingStatus.FAILED)

    def test_only_authors_depending_on_a_bad_alias_get_individual_requests(self):
        self.people(8)

        def handler(request, _client):
            if urlsplit(request.url).path == "/authors":
                edges = [("A1", "A2"), ("A1", "A3"), ("A4", "A1"), ("A5", "A2"), ("A6", None), ("A8", "A6")]
                return 200, {}, {
                    "results": [self.author(key) for key in ("A2", "A3", "A7")],
                    "meta": {"merged_ids": [{"id": key, "merge_into_id": target} for key, target in edges]},
                }
            return self.respond(request)

        self.handler = handler
        PersonsStage(self.prepared, self.raw, self.config).run()
        self.assertCountEqual([urlsplit(req.url).path for req, _, _ in self.requests],
                              ["/authors", "/authors/A1", "/authors/A4", "/authors/A6", "/authors/A8"])
        rows = self.rows()
        self.assertEqual(rows["P5"].name_raw, "Author A2")
        self.assertTrue(all(p.processing["openalex_author"].status == ProcessingStatus.COMPLETED for p in rows.values()))

    def test_malformed_and_conflicting_results_leave_valid_batch_members_intact(self):
        self.people(3)

        def handler(request, _client):
            path = urlsplit(request.url).path
            if path == "/authors":
                return 200, {}, {"results": [
                    self.author("A1"), {"display_name": "Missing ID"},
                    self.author("A3"), self.author("A3") | {"display_name": "Conflicting author"},
                    self.author("A3"),
                ]}
            if path.endswith("/A3"):
                return 404, {}, {}
            return self.respond(request)

        self.handler = handler
        PersonsStage(self.prepared, self.raw, self.config).run()
        rows = self.rows()
        self.assertEqual({urlsplit(request.url).path for request, _, _ in self.requests},
                         {"/authors", "/authors/A2", "/authors/A3"})
        self.assertEqual(rows["P1"].name_raw, "Author A1")
        self.assertEqual(rows["P2"].name_raw, "Author A2")
        self.assertEqual(rows["P3"].processing["openalex_author"].status, ProcessingStatus.FAILED)
        self.assertIsNone(rows["P3"].name_raw)

    def test_invalid_id_in_a_batch_is_isolated_by_single_lookups(self):
        self.people(2)

        def handler(request, _client):
            parsed = urlsplit(request.url)
            if parsed.path == "/authors" or parsed.path.endswith("/A1"):
                return 400, {}, {"error": "invalid author id"}
            return self.respond(request)

        self.handler = handler
        PersonsStage(self.prepared, self.raw, self.config).run()
        rows = self.rows()
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(rows["P1"].processing["openalex_author"].status, ProcessingStatus.FAILED)
        self.assertEqual(rows["P2"].processing["openalex_author"].status, ProcessingStatus.COMPLETED)

    def test_a_service_failure_does_not_expand_into_individual_requests(self):
        self.people(2)
        self.handler = lambda _request, _client: (503, {"Retry-After": "0"}, {})
        PersonsStage(self.prepared, self.raw, self.config).run()
        self.assertEqual(len(self.requests), 4)
        self.assertTrue(all(urlsplit(request.url).path == "/authors" for request, _thread, _session in self.requests))
        self.assertTrue(all(person.processing["openalex_author"].status == ProcessingStatus.FAILED
                            for person in self.rows().values()))

    def test_cancellation_retains_source_results_and_resume_only_fetches_unfinished_orcid(self):
        self.people(2)
        self.with_orcid = True
        record_path = f"/v3.0/{self.ORCIDS['A1']}/record"

        def handler(request, client):
            if urlsplit(request.url).hostname == "pub.orcid.org" and record_path in request.url:
                if not client._cancel.wait(2):
                    raise AssertionError("slow ORCID request did not stop")
                raise CancelledError
            return self.respond(request)

        def progress(label, _done, _total):
            if label == "Authors: ORCID" and "orcid" in self.rows()["P2"].processing:
                raise RuntimeError("stop requested")

        self.handler = handler
        stage = PersonsStage(self.prepared, self.raw, self.config, on_progress=progress)
        stage.REPORT_SECONDS = 0
        with self.assertRaisesRegex(RuntimeError, "stop requested"):
            stage.run()
        rows = self.rows()
        self.assertTrue(all(person.processing["openalex_author"].status == ProcessingStatus.COMPLETED for person in rows.values()))
        self.assertNotIn("orcid", rows["P1"].processing)
        self.assertEqual(rows["P2"].processing["orcid"].status, ProcessingStatus.COMPLETED)
        self.assertTrue(all(client.close.call_count == 1 for client in self.clients))
        self.handler = None
        self.requests.clear()
        PersonsStage(self.prepared, self.raw, self.config).run()
        self.assertEqual(len(self.requests), 1)
        self.assertIn(record_path, self.requests[0][0].url)
        self.assertEqual(self.rows()["P2"].processing["orcid"].attempts, 1)

    def test_progress_can_stop_a_retry_wait_without_failing_the_author(self):
        self.prepared.write_models("persons", [Person(id="P1", orcid="A1", is_itmo=False)])
        self.handler = lambda _request, _client: (503, {"Retry-After": "2"}, {})

        def progress(_label, _done, _total):
            if self.requests:
                raise RuntimeError("stop requested")

        stage = PersonsStage(self.prepared, self.raw, self.config, on_progress=progress)
        stage.REPORT_SECONDS = 0
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "stop requested"):
            stage.run()
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn("orcid", self.rows()["P1"].processing)

    def test_selection_does_not_fetch_or_remove_unselected_people(self):
        self.people(3)
        PreparedStore(self.db, "other").upsert_models("persons", [Person(id="P4", openalex_id="A4", is_itmo=False)])
        selection = PreparedSelection("persons", frozenset({"P2"}))
        PersonsStage(self.prepared, self.raw, self.config, selection=selection).run()
        self.assertEqual(len(self.requests), 1)
        rows = self.rows()
        self.assertEqual(set(rows), {"P1", "P2", "P3"})
        self.assertEqual(rows["P1"].processing, {})
        self.assertEqual(rows["P3"].processing, {})
        self.assertEqual(self.db.persons.find_one({"_id": "P4"})["groups"], ["other"])

    def test_storage_failure_closes_the_window_and_all_sessions(self):
        self.people(201)

        def handler(request, client):
            query = parse_qs(urlsplit(request.url).query).get("filter", [""])[0]
            if query.startswith("openalex_id:A101|"):
                if not client._cancel.wait(2):
                    raise AssertionError("pending request did not stop")
                raise CancelledError
            return self.respond(request)

        self.handler = handler
        with (mock.patch.object(self.prepared, "upsert_models", side_effect=RuntimeError("storage unavailable")),
              self.assertRaisesRegex(RuntimeError, "storage unavailable")):
            PersonsStage(self.prepared, self.raw, self.config).run()
        self.assertTrue(self.clients)
        self.assertTrue(all(client.close.call_count == 1 and client._cancel.is_set() for client in self.clients))
        self.assertTrue(all(not person.processing for person in self.rows().values()))

    def test_force_and_changed_source_keys_do_not_reuse_old_completion(self):
        self.people(1)
        self.with_orcid = True
        PersonsStage(self.prepared, self.raw, self.config).run()
        self.requests.clear()
        PersonsStage(self.prepared, self.raw, self.config).run()
        self.assertEqual(self.requests, [])
        person = self.rows()["P1"]
        person.orcid = "replacement"
        self.prepared.upsert_models("persons", [person])
        PersonsStage(self.prepared, self.raw, self.config).run()
        self.assertEqual(len(self.requests), 1)
        self.assertIn("/replacement/record", self.requests[0][0].url)
        self.requests.clear()
        PersonsStage(self.prepared, self.raw, self.config, force=True).run()
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.rows()["P1"].processing["openalex_author"].attempts, 2)
        self.assertEqual(self.rows()["P1"].processing["orcid"].attempts, 3)

    def test_raw_storage_failure_cannot_mark_an_author_completed(self):
        self.people(1)
        with mock.patch.object(self.raw, "append", side_effect=RuntimeError("raw storage failed")):
            PersonsStage(self.prepared, self.raw, self.config).run()
        person = self.rows()["P1"]
        self.assertEqual(person.processing["openalex_author"].status, ProcessingStatus.FAILED)
        self.assertIsNone(person.name_raw)

    def test_invalid_openalex_configuration_does_not_fail_every_author(self):
        self.people(2)
        config = replace(self.config, openalex_rps=0)
        with self.assertRaisesRegex(ValueError, "requests_per_second"):
            PersonsStage(self.prepared, self.raw, config).run()
        self.assertEqual(self.requests, [])
        self.assertTrue(all(not person.processing for person in self.rows().values()))

    def test_invalid_openalex_profile_cannot_partly_replace_an_author(self):
        for changes in ({"affiliations": [None]}, {"display_name": {"name": "Not a string"}}):
            with self.subTest(changes=changes):
                self.prepared.write_models("persons", [
                    Person(id="P1", openalex_id="A1", name_raw="Known name", is_itmo=False),
                ])
                self.handler = mock.Mock(return_value=(200, {}, self.author("A1") | changes))
                PersonsStage(self.prepared, self.raw, self.config).run()
                person = self.rows()["P1"]
                self.assertEqual(person.processing["openalex_author"].status, ProcessingStatus.FAILED)
                self.assertEqual(person.name_raw, "Known name")

    def test_failed_orcid_profile_does_not_replace_emails(self):
        self.prepared.write_models("persons", [
            Person(id="P1", orcid="A1", email="known@example.org", emails=["known@example.org"], is_itmo=False),
        ])

        def malformed(request, _client):
            status, headers, payload = self.respond(request)
            payload["activities-summary"] = {"employments": {"affiliation-group": [None]}}
            return status, headers, payload

        self.handler = malformed
        PersonsStage(self.prepared, self.raw, self.config).run()
        person = self.rows()["P1"]
        self.assertEqual(person.processing["orcid"].status, ProcessingStatus.FAILED)
        self.assertEqual(person.emails, ["known@example.org"])

    def test_invalid_worker_counts_are_rejected(self):
        for count in (0, -1, True, 1.5):
            with self.subTest(count=count), self.assertRaises(ValueError):
                replace(self.config, persons_concurrency=count)
