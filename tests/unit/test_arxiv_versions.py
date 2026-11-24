import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import mongomock
import pymupdf as fitz

from pauk.ingest.aggregate import build_publication
from pauk.models import ClassificationStatus, CodeLink, LinkOccurrence, Publication, PublicationSourceRecord, RepoLink
from pauk.models.processing import ProcessingState, ProcessingStatus
from pauk.models.publication import ArxivSource
from pauk.pipeline.normalize import OpenAlexNormalizer
from pauk.pipeline.stages import ALL_STAGES
from pauk.pipeline.stages.arxiv_versions import (
    ARXIV_HOSTS,
    ArxivVersionsStage,
    bare_doi,
    has_arxiv_link,
)
from pauk.pipeline.stages.base import PreparedSelection
from pauk.pipeline.stages.code_links import CodeLinksStage
from pauk.pipeline.stages.dedup import _merge_publication
from pauk.pipeline.stages.link_relevance import LinkRelevanceStage
from pauk.settings import ARXIV_RPS, Settings
from pauk.sources.base import HttpClient, HttpRequestError
from pauk.sources.rate_limit import HostRateLimiter
from pauk.sources.semantic_scholar import (
    BATCH_URL,
    MAX_BATCH,
    SemanticScholarClient,
    arxiv_id,
)
from pauk.storage import PdfStore, PreparedStore, RawStore
from tests.unit.test_http_rate_limit import _attach
from tests.unit.test_rate_limit import _Clock

STAGE_CLIENT = "pauk.pipeline.stages.arxiv_versions.SemanticScholarClient"


def _pdf(*pages: str) -> bytes:
    with fitz.open() as doc:
        for text in pages:
            doc.new_page().insert_text((72, 72), text)
        return doc.tobytes()


def _record(identifier: str | None) -> dict:
    return {"paperId": "p", "externalIds": {"DOI": "x", **({"ArXiv": identifier} if identifier else {})}}


class SemanticScholarClientTest(unittest.TestCase):
    def test_batch_posts_prefixed_dois_and_returns_records_in_order(self):
        client = SemanticScholarClient(timeout=5)
        records = [_record("2201.06114"), None]
        with mock.patch.object(client, "request_json", return_value=records) as request:
            self.assertEqual(client.papers_by_doi(["10.1/a", "10.1/b"]), records)
        method, url = request.call_args.args
        self.assertEqual((method, url), ("POST", BATCH_URL))
        self.assertEqual(request.call_args.kwargs["json"], {"ids": ["DOI:10.1/a", "DOI:10.1/b"]})
        self.assertEqual(request.call_args.kwargs["params"], {"fields": "externalIds"})

    def test_more_dois_than_one_batch_are_refused(self):
        client = SemanticScholarClient(timeout=5)
        with mock.patch.object(client, "request_json") as request, self.assertRaises(ValueError):
            client.papers_by_doi([f"10.1/{n}" for n in range(MAX_BATCH + 1)])
        request.assert_not_called()

    def test_a_response_that_is_not_one_record_per_doi_is_an_error(self):
        client = SemanticScholarClient(timeout=5)
        for payload in ([None], {"error": "bad"}):
            with (self.subTest(payload=payload),
                  mock.patch.object(client, "request_json", return_value=payload),
                  self.assertRaises(ValueError)):
                client.papers_by_doi(["10.1/a", "10.1/b"])

    def test_api_key_goes_into_its_header_only_when_set(self):
        self.assertEqual(SemanticScholarClient(5, "secret").session.headers["x-api-key"], "secret")
        self.assertNotIn("x-api-key", SemanticScholarClient(5).session.headers)

    def test_arxiv_id_is_read_from_external_ids(self):
        self.assertEqual(arxiv_id(_record("2201.06114")), "2201.06114")
        self.assertEqual(arxiv_id(_record("cond-mat/0501234")), "cond-mat/0501234")
        for record in (None, {}, {"externalIds": None}, _record(None), _record("  ")):
            with self.subTest(record=record):
                self.assertIsNone(arxiv_id(record))


class ArxivDownloadPaceTest(unittest.TestCase):
    def test_arxiv_downloads_are_paced_for_every_client(self):
        limits = HttpClient(timeout=5).rate_limits
        for host in ARXIV_HOSTS:
            with self.subTest(host=host):
                self.assertEqual(limits[host].requests_per_second, ARXIV_RPS)
                self.assertEqual(1 / limits[host].requests_per_second, 15)
                self.assertEqual(limits[host].max_concurrent, 1)

    def test_arxiv_request_starts_include_transfer_time_in_the_interval(self) -> None:
        """Space short requests by 15 seconds without delaying an already slow transfer."""
        for host in ARXIV_HOSTS:
            for duration in (1, 20):
                with self.subTest(host=host, duration=duration):
                    clock = _Clock()
                    limiter = HostRateLimiter(clock=clock)
                    client = HttpClient(timeout=5, rate_limiter=limiter)
                    starts = []

                    def download(
                        _request: object,
                        starts: list[float] = starts,
                        clock: _Clock = clock,
                        duration: int = duration,
                    ) -> tuple[int, dict, dict]:
                        starts.append(clock())
                        clock.now += duration
                        return 200, {}, {}

                    _attach(client, download)
                    self.addCleanup(client.session.close)
                    state = limiter._state(host, client.rate_limits[host])
                    with mock.patch.object(state.condition, "wait", side_effect=clock.wait):
                        client.get_bytes(f"https://{host}/pdf/2201.06114")
                        client.get_bytes(f"https://{host}/pdf/2201.06115")
                    self.assertEqual(starts[1] - starts[0], max(15, duration))


class ArxivVersionsStageTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        self.prepared = PreparedStore(self.db, "sample")
        self.raw = RawStore(self.db, "sample")
        patcher = mock.patch(STAGE_CLIENT)
        self.client_cls = patcher.start()
        self.addCleanup(patcher.stop)
        self.client = self.client_cls.return_value

    def _run(self, **kwargs) -> dict[str, Publication]:
        ArxivVersionsStage(self.prepared, self.raw, **kwargs).run()
        return {row.id: row for row in self.prepared.read_models("publications", Publication)}

    def _queried(self) -> list[list[str]]:
        return [call.args[0] for call in self.client.papers_by_doi.call_args_list]

    def test_settings_rate_reaches_the_semantic_scholar_client(self) -> None:
        """Forward explicitly configured Semantic Scholar quotas independently of the environment."""
        for config, expected in ((Settings(semantic_scholar_rps=1), 1), (Settings(semantic_scholar_rps=2.5), 2.5)):
            with self.subTest(rate=expected):
                limiter = HostRateLimiter(clock=_Clock())
                clients = []

                def create_client(
                    timeout: int,
                    api_key: str,
                    *,
                    requests_per_second: float,
                    clients: list[SemanticScholarClient] = clients,
                ) -> SemanticScholarClient:
                    client = SemanticScholarClient(timeout, api_key, requests_per_second=requests_per_second)
                    clients.append(client)
                    self.addCleanup(client.session.close)
                    return client

                with (
                    mock.patch("pauk.sources.base.RATE_LIMITER", limiter),
                    mock.patch(STAGE_CLIENT, side_effect=create_client),
                    mock.patch.object(SemanticScholarClient, "papers_by_doi", return_value=[]),
                ):
                    ArxivVersionsStage(self.prepared, self.raw, config=config).run()
                self.assertEqual(len(clients), 1)
                client = clients[0]
                limit = client.rate_limits["api.semanticscholar.org"]
                self.assertEqual(config.semantic_scholar_rps, expected)
                self.assertEqual(limit.requests_per_second, expected)
                self.assertEqual(limit.max_concurrent, 1)
                self.assertIs(client.rate_limiter, limiter)

    def test_the_stage_runs_before_code_links(self):
        names = [stage.name for stage in ALL_STAGES]
        self.assertLess(names.index("arxiv_versions"), names.index("code_links"))

    def test_an_arxiv_link_is_appended_after_the_existing_ones(self):
        self.prepared.write_models("publications", [Publication(
            id="W1", title="t", doi="https://doi.org/10.1103/PRXQuantum.3.040307",
            pdf_urls=["https://journals.aps.org/paper.pdf"],
        )])
        self.client.papers_by_doi.return_value = [_record("2201.06114")]
        rows = self._run()
        self.assertEqual(self._queried(), [["10.1103/PRXQuantum.3.040307"]])
        self.assertEqual(rows["W1"].pdf_urls, ["https://journals.aps.org/paper.pdf",
                                               "https://arxiv.org/pdf/2201.06114"])
        state = rows["W1"].processing["arxiv_versions"]
        self.assertEqual((state.status, state.result_count), (ProcessingStatus.COMPLETED, 1))
        self.assertEqual(self.db.raw.count_documents({"source": "semantic_scholar"}), 1)

    def test_only_papers_without_text_with_a_doi_and_no_arxiv_link_are_looked_up(self):
        self.prepared.write_models("publications", [
            Publication(id="W1", title="t", doi="https://doi.org/10.1/a"),
            Publication(id="W2", title="t", doi="https://doi.org/10.1/b", full_text="text"),
            Publication(id="W3", title="t"),
            Publication(id="W4", title="t", doi="https://doi.org/10.1/d",
                        pdf_urls=["https://arxiv.org/pdf/2101.00001v2"]),
        ])
        self.client.papers_by_doi.return_value = [None]
        rows = self._run()
        self.assertEqual(self._queried(), [["10.1/a"]])
        for skipped in ("W2", "W3", "W4"):
            self.assertNotIn("arxiv_versions", rows[skipped].processing)

    def test_an_unknown_doi_leaves_the_links_alone(self):
        self.prepared.write_models("publications", [Publication(id="W1", title="t", doi="10.1/a")])
        self.client.papers_by_doi.return_value = [_record(None)]
        rows = self._run()
        self.assertEqual(rows["W1"].pdf_urls, [])
        self.assertEqual(rows["W1"].processing["arxiv_versions"].status, ProcessingStatus.COMPLETED_EMPTY)

    def test_a_new_link_sends_the_paper_back_to_code_links(self):
        done = ProcessingState(status=ProcessingStatus.COMPLETED_EMPTY)
        self.prepared.write_models("publications", [
            Publication(id="W1", title="t", doi="10.1/a", processing={"code_links": done}),
            Publication(id="W2", title="t", doi="10.1/b", processing={"code_links": done}),
        ])
        self.client.papers_by_doi.return_value = [_record("2201.06114"), None]
        rows = self._run()
        self.assertNotIn("code_links", rows["W1"].processing)
        self.assertEqual(rows["W2"].processing["code_links"].status, ProcessingStatus.COMPLETED_EMPTY)

    def test_a_paper_that_already_has_links_gets_independent_pending_extraction(self):
        done = ProcessingState(status=ProcessingStatus.COMPLETED, result_count=1)
        self.prepared.write_models("publications", [
            Publication(id="W1", title="t", doi="10.1/a", processing={"code_links": done}),
        ])
        self.prepared.write_models("repo_links", [
            RepoLink(publication_id="W1", links=[CodeLink(url="https://github.com/org/repo")]),
        ])
        self.client.papers_by_doi.return_value = [_record("2201.06114")]
        rows = self._run()
        self.assertEqual(rows["W1"].pdf_urls, ["https://arxiv.org/pdf/2201.06114"])
        self.assertEqual(rows["W1"].processing["code_links"].status, ProcessingStatus.COMPLETED)
        self.assertEqual(rows["W1"].arxiv_sources, [ArxivSource(url=rows["W1"].pdf_urls[0])])

    def test_source_state_survives_normalize_and_merge(self) -> None:
        url = "https://arxiv.org/pdf/2201.06114"
        pending = ArxivSource(url=url)
        done = ArxivSource(url=url, state=ProcessingState(status=ProcessingStatus.COMPLETED))
        current = Publication(id="W1", title="Paper", pdf_urls=[url], arxiv_sources=[pending])
        other = Publication(id="W2", title="Paper", pdf_urls=[url], arxiv_sources=[done])
        merged = _merge_publication(current, other)
        self.assertEqual(merged.arxiv_sources, [done])
        self.assertEqual(_merge_publication(merged, Publication(
            id="W3", title="Paper", pdf_urls=[url], arxiv_sources=[pending],
        )).arxiv_sources, [done])
        self.prepared.write_models("publications", [merged])
        self.raw.append("openalex_works", {"id": "https://openalex.org/W1", "title": "Paper"}, {})
        OpenAlexNormalizer(self.raw, self.prepared).run()
        result, = self.prepared.read_models("publications", Publication)
        self.assertEqual(result.arxiv_sources, [done])

    def test_text_of_only_whitespace_counts_as_no_text(self):
        self.prepared.write_models("publications", [
            Publication(id="W1", title="t", doi="10.1/a", full_text=" \n\n "),
        ])
        self.client.papers_by_doi.return_value = [None]
        self._run()
        self.assertEqual(self._queried(), [["10.1/a"]])

    def test_a_malformed_response_fails_the_batch(self):
        self.prepared.write_models("publications", [Publication(id="W1", title="t", doi="10.1/a")])
        self.client.papers_by_doi.side_effect = ValueError("does not match")
        rows = self._run()
        self.assertEqual(rows["W1"].processing["arxiv_versions"].status, ProcessingStatus.FAILED)

    def test_an_unknown_doi_is_not_asked_again(self):
        self.prepared.write_models("publications", [Publication(id="W1", title="t", doi="10.1/a")])
        self.client.papers_by_doi.return_value = [None]
        self._run()
        self._run()
        self.assertEqual(len(self._queried()), 1)

    def test_the_result_counts_do_not_collide_with_other_stages(self):
        self.prepared.write_models("publications", [
            Publication(id="W1", title="t", doi="10.1/a"),
            Publication(id="W2", title="t", doi="10.1/b"),
        ])
        self.client.papers_by_doi.return_value = [_record("2201.06114"), None]
        result = ArxivVersionsStage(self.prepared, self.raw).run()
        self.assertEqual(result, {"arxiv_looked_up": 2, "arxiv_found": 1, "arxiv_failed": 0})

    def test_a_failed_batch_is_retried_on_the_next_run_and_nothing_else_changes(self):
        done = ProcessingState(status=ProcessingStatus.COMPLETED_EMPTY)
        self.prepared.write_models("publications", [
            Publication(id="W1", title="t", doi="10.1/a", processing={"code_links": done}),
        ])
        self.client.papers_by_doi.side_effect = HttpRequestError("POST", BATCH_URL, status_code=429)
        rows = self._run()
        state = rows["W1"].processing["arxiv_versions"]
        self.assertEqual(state.status, ProcessingStatus.FAILED)
        self.assertIn("429", state.error)
        self.assertEqual(rows["W1"].processing["code_links"].status, ProcessingStatus.COMPLETED_EMPTY)
        self.client.papers_by_doi.side_effect = None
        self.client.papers_by_doi.return_value = [_record("2201.06114")]
        rows = self._run()
        self.assertEqual(rows["W1"].processing["arxiv_versions"].attempts, 2)
        self.assertEqual(rows["W1"].pdf_urls, ["https://arxiv.org/pdf/2201.06114"])

    def test_a_second_run_asks_nothing_and_adds_no_duplicate(self):
        self.prepared.write_models("publications", [Publication(id="W1", title="t", doi="10.1/a")])
        self.client.papers_by_doi.return_value = [_record("2201.06114")]
        self._run()
        rows = self._run()
        self.assertEqual(len(self._queried()), 1)
        self.assertEqual(rows["W1"].pdf_urls, ["https://arxiv.org/pdf/2201.06114"])

    def test_force_does_not_duplicate_a_link_already_added(self):
        self.prepared.write_models("publications", [Publication(id="W1", title="t", doi="10.1/a")])
        self.client.papers_by_doi.return_value = [_record("2201.06114")]
        self._run()
        rows = self._run(force=True)
        self.assertEqual(len(self._queried()), 1)
        self.assertEqual(rows["W1"].pdf_urls, ["https://arxiv.org/pdf/2201.06114"])

    def test_papers_are_sent_in_batches(self):
        self.prepared.write_models("publications", [
            Publication(id=f"W{n}", title="t", doi=f"10.1/{n}") for n in range(5)
        ])
        self.client.papers_by_doi.side_effect = lambda dois: [None] * len(dois)
        with mock.patch("pauk.pipeline.stages.arxiv_versions.MAX_BATCH", 2):
            self._run()
        self.assertEqual([len(batch) for batch in self._queried()], [2, 2, 1])

    def test_doi_resolver_prefixes_are_stripped(self):
        self.assertEqual(bare_doi("https://doi.org/10.1/A"), "10.1/A")
        self.assertEqual(bare_doi("http://dx.doi.org/10.1/a"), "10.1/a")
        self.assertEqual(bare_doi("10.1/a"), "10.1/a")

    def test_arxiv_links_are_recognised_by_host(self):
        for url, expected in (("https://arxiv.org/abs/2101.00001", True),
                              ("https://export.arxiv.org/pdf/2101.00001", True),
                              ("https://example.org/arxiv.org/x.pdf", False)):
            with self.subTest(url=url):
                self.assertEqual(has_arxiv_link(Publication(id="W", title="t", pdf_urls=[url])), expected)


class ArxivThenCodeLinksTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Settings(data_dir=Path(tmp.name) / "data", pdf_crawler_url="")
        self.prepared = PreparedStore(self.db, "sample")
        self.raw = RawStore(self.db, "sample")

    @mock.patch("pauk.pipeline.stages.code_links.HttpClient")
    def test_valid_cached_preprint_does_not_fetch_a_pdf(self, http: mock.Mock) -> None:
        """Read cached PDF text without making an HTTP PDF request."""
        self.prepared.write_models(
            "publications",
            [
                Publication(
                    id="W1",
                    title="t",
                    doi="10.1/a",
                    pdf_urls=["https://arxiv.org/pdf/2201.06114"],
                )
            ],
        )
        PdfStore(self.db, self.config.pdf_dir).save("W1", _pdf("https://github.com/org/cached"))
        CodeLinksStage(self.prepared, self.raw, config=self.config).run()
        http.return_value.get_bytes.assert_not_called()
        pub = next(iter(self.prepared.read_models("publications", Publication)))
        self.assertIn("github.com/org/cached", pub.full_text)
        self.assertEqual(pub.processing["code_links"].status, ProcessingStatus.COMPLETED)

    @mock.patch("pauk.pipeline.stages.code_links.HttpClient")
    @mock.patch(STAGE_CLIENT)
    def test_code_links_reads_the_preprint_when_the_publisher_refuses(self, s2, http):
        publisher = "https://publisher.example/paper.pdf"
        self.prepared.write_models("publications", [Publication(
            id="W1", title="t", doi="10.1/a", pdf_urls=[publisher],
            processing={"code_links": ProcessingState(status=ProcessingStatus.FAILED)},
        )])
        s2.return_value.papers_by_doi.return_value = [_record("2201.06114")]

        arxiv_url = "https://arxiv.org/pdf/2201.06114"
        http.return_value.get_bytes.side_effect = HttpRequestError("GET", publisher, status_code=403)
        http.return_value.get_bytes_with_url.return_value = (_pdf("Our code: https://github.com/org/repo"), arxiv_url)
        ArxivVersionsStage(self.prepared, self.raw, config=self.config).run()
        CodeLinksStage(self.prepared, self.raw, config=self.config).run()
        pub = next(iter(self.prepared.read_models("publications", Publication)))
        self.assertIn("github.com/org/repo", pub.full_text)
        self.assertEqual(pub.processing["code_links"].status, ProcessingStatus.COMPLETED)
        links = next(iter(self.prepared.read_models("repo_links", RepoLink)))
        self.assertEqual([link.url for link in links.links], ["https://github.com/org/repo"])
        self.assertEqual(http.return_value.mock_calls,
                         [mock.call.get_bytes(publisher), mock.call.get_bytes_with_url(arxiv_url)])

    def _discover(self, identifier: str = "2201.06114") -> str:
        with mock.patch(STAGE_CLIENT) as s2:
            s2.return_value.papers_by_doi.return_value = [_record(identifier)]
            ArxivVersionsStage(self.prepared, self.raw, config=self.config, selection=self.selection).run()
        return f"https://arxiv.org/pdf/{identifier}"

    @property
    def selection(self) -> PreparedSelection:
        return PreparedSelection("publications", frozenset({"W1"}))

    def _extract(self, **kwargs: bool) -> dict[str, int]:
        return CodeLinksStage(self.prepared, self.raw, config=self.config, selection=self.selection, **kwargs).run()

    def _work(self) -> Publication:
        return next(pub for pub in self.prepared.read_models("publications", Publication) if pub.id == "W1")

    def _source_state(self, url: str) -> ProcessingState:
        return next(source.state for source in self._work().arxiv_sources if source.url == url)

    def _links(self) -> dict[str, CodeLink]:
        row = next(row for row in self.prepared.read_models("repo_links", RepoLink) if row.publication_id == "W1")
        return {link.url: link for link in row.links}

    @mock.patch("pauk.pipeline.stages.code_links.HttpClient")
    def test_journal_evidence_uses_the_existing_preprint_id_and_citation_page(self, http: mock.Mock) -> None:
        self.prepared.write_models("publications", [
            Publication(id="W1", title="Journal", doi="10.1/journal"),
            Publication(id="W2", title="Preprint", doi="https://doi.org/10.48550/arXiv.2201.06114",
                        pdf_urls=["https://arxiv.org/pdf/2201.06114v2"]),
        ])
        store = PdfStore(self.db, self.config.pdf_dir)
        store.save("W1", _pdf("Publisher text"))
        store.save("W2", _pdf("Preprint", "Our code [1].\nReferences\n[1] https://github.com/org/preprint"),
                   source_url="https://arxiv.org/pdf/2201.06114v2")
        url = self._discover()

        self._extract()

        self.assertEqual(http.return_value.mock_calls, [])
        self.assertEqual(self._work().full_text.strip(), "Publisher text")
        self.assertEqual(self._source_state(url).status, ProcessingStatus.COMPLETED)
        occurrences = self._links()["https://github.com/org/preprint"].occurrences
        self.assertEqual({(o.pdf_id, o.page_number, o.source) for o in occurrences},
                         {("W2", 2, "citation"), ("W2", 2, "reference")})
        self.assertEqual({pub.id for pub in self.prepared.read_models("publications", Publication)}, {"W1", "W2"})

    @mock.patch("pauk.pipeline.stages.code_links.HttpClient")
    def test_uncached_compatible_preprint_is_downloaded_once_for_both_records(self, http: mock.Mock) -> None:
        preprint_url = "https://arxiv.org/pdf/2201.06114v2"
        self.prepared.write_models("publications", [
            Publication(id="W1", title="Journal", doi="10.1/journal"),
            Publication(id="W2", title="Preprint", type="preprint", pdf_urls=[preprint_url]),
        ])
        self._discover()
        http.return_value.get_bytes_with_url.return_value = (_pdf("https://github.com/org/preprint"), preprint_url)

        CodeLinksStage(self.prepared, self.raw, config=self.config).run()

        http.return_value.get_bytes_with_url.assert_called_once_with(preprint_url)
        store = PdfStore(self.db, self.config.pdf_dir)
        self.assertFalse(store.exists("W1"))
        self.assertEqual(store.read_with_source("W2")[1], preprint_url)
        self.assertEqual(self._links()["https://github.com/org/preprint"].occurrences[0].pdf_id, "W2")

    @mock.patch("pauk.pipeline.stages.link_relevance.llm_client")
    @mock.patch("pauk.pipeline.stages.code_links.HttpClient")
    def test_completed_journal_rejudges_only_new_or_changed_automatic_evidence(
        self, http: mock.Mock, llm: mock.Mock,
    ) -> None:
        done = ProcessingState(status=ProcessingStatus.COMPLETED)
        self.prepared.write_models("publications", [Publication(
            id="W1", title="Journal", doi="10.1/journal", processing={"code_links": done, "link_relevance": done},
        )])
        irrelevant = CodeLink(url="https://github.com/org/irrelevant", is_relevant=False, llm_reason="prior tool",
                              occurrences=[LinkOccurrence(source="pdf_text", pdf_id="W1", page_number=3)],
                              classification_status=ClassificationStatus.CLASSIFIED)
        manual = CodeLink(url="https://github.com/org/manual", is_relevant=True,
                          classification_status=ClassificationStatus.CLASSIFIED)
        same = irrelevant.model_copy(
            update={"url": "http://www.github.com/Org/Shared.git", "availability": "available"},
        )
        self.prepared.write_models("repo_links", [RepoLink(publication_id="W1", links=[irrelevant, manual, same])])
        source = self._discover()
        http.return_value.get_bytes_with_url.return_value = (
            _pdf("https://github.com/org/shared\nhttps://github.com/org/new\nhttps://github.com/org/manual"), source,
        )
        llm.return_value.configure_mock(model="test", last_response=None, last_usage=None,
                                       **{"chat_json.return_value": {"is_authors_artifact": True, "confidence": 0.9}})

        self._extract()

        self.assertEqual(self._links()[irrelevant.url], irrelevant)
        self.assertEqual(self._links()[manual.url], manual)
        updated = self._links()[same.url]
        self.assertEqual(updated.classification_status, ClassificationStatus.PENDING)
        self.assertIsNone(updated.is_relevant)
        self.assertEqual(updated.availability, "available")
        self.assertEqual([o.pdf_id for o in updated.occurrences], ["W1", source])
        self.assertNotIn("link_relevance", self._work().processing)
        LinkRelevanceStage(self.prepared, self.raw, config=self.config).run()
        self.assertEqual(llm.return_value.chat_json.call_count, 2)
        links_after = self._links()
        self.assertEqual(links_after[irrelevant.url], irrelevant)
        self.assertEqual(links_after[manual.url], manual)
        self.assertTrue(links_after[same.url].is_relevant)
        self.assertTrue(links_after["https://github.com/org/new"].is_relevant)
        self.assertEqual(self._extract()["publications"], 0)
        self._extract(force=True)
        self.assertEqual(self._links(), links_after)
        http.return_value.get_bytes_with_url.assert_called_once_with(source)

    @mock.patch("pauk.pipeline.stages.code_links.HttpClient")
    def test_ambiguous_mismatched_and_journal_fallback_caches_are_not_borrowed(self, http: mock.Mock) -> None:
        cases = ("duplicates", "wrong revision", "copied by discovery", "unrelated URL", "unknown legacy provenance")
        for index, case in enumerate(cases, 1):
            with self.subTest(case=case):
                identifier = f"2201.0611{index}"
                expected = f"https://arxiv.org/pdf/{identifier}v2"
                preprint = Publication(id="W2", title="Same title", type="preprint", pdf_urls=[expected])
                others = [preprint]
                if case == "duplicates":
                    others.append(preprint.model_copy(update={"id": "W3"}))
                elif case == "wrong revision":
                    preprint.pdf_urls = [expected.removesuffix("v2") + "v1"]
                elif case == "copied by discovery":
                    preprint.doi = "10.1/another-journal"
                    preprint.arxiv_sources.append(ArxivSource(url=expected))
                elif case == "unrelated URL":
                    preprint.pdf_urls = [f"https://example.org/arxiv.org/pdf/{identifier}v2"]
                self.prepared.write_models("publications", [
                    Publication(id="W1", title="Same title", doi="10.1/journal"), *others,
                ])
                self.prepared.write_models("repo_links", [])
                store = PdfStore(self.db, self.config.pdf_dir)
                provenance = None if case == "unknown legacy provenance" else expected
                store.save("W2", _pdf("https://github.com/org/not-the-source"), source_url=provenance)
                http.reset_mock()
                http.return_value.get_bytes_with_url.return_value = (
                    _pdf("https://github.com/org/true-source"), expected,
                )
                self._discover(identifier + "v2")

                self._extract()

                http.return_value.get_bytes_with_url.assert_called_once_with(expected)
                link, = self._links().values()
                self.assertEqual(link.url, "https://github.com/org/true-source")
                self.assertEqual({occurrence.pdf_id for occurrence in link.occurrences}, {expected})
                self.assertFalse(store.exists("W1"))

    @mock.patch("pauk.pipeline.stages.code_links.HttpClient")
    def test_unreadable_preprint_cache_and_failed_download_keep_good_evidence(self, http: mock.Mock) -> None:
        old = CodeLink(url="https://github.com/org/old", is_relevant=False,
                       classification_status=ClassificationStatus.CLASSIFIED)
        done = ProcessingState(status=ProcessingStatus.COMPLETED)
        self.prepared.write_models("publications", [
            Publication(id="W1", title="Journal", doi="10.1/journal", processing={"code_links": done}),
            Publication(id="W2", title="Preprint", type="preprint", pdf_urls=["https://arxiv.org/pdf/2201.06114"]),
        ])
        self.prepared.write_models("repo_links", [RepoLink(publication_id="W1", links=[old])])
        store = PdfStore(self.db, self.config.pdf_dir)
        store.save("W2", b"unreadable")
        url = self._discover()
        work = self._work()
        work.full_text = "Previously extracted text"
        self.prepared.upsert_models("publications", [work])
        http.return_value.get_bytes_with_url.side_effect = [
            HttpRequestError("GET", url, status_code=503), (_pdf("https://github.com/org/new"), url),
        ]

        self._extract()
        self.assertEqual(self._links(), {old.url: old})
        self.assertEqual(self._source_state(url).status, ProcessingStatus.FAILED)
        self._extract()

        self.assertEqual(self._work().full_text, "Previously extracted text")
        self.assertEqual(self._source_state(url).attempts, 2)
        self.assertEqual(self._source_state(url).status, ProcessingStatus.COMPLETED)
        self.assertEqual(self._links()[old.url], old)
        self.assertEqual(store.read("W2"), b"unreadable")
        self.assertEqual(self._links()["https://github.com/org/new"].occurrences[0].pdf_id, url)
        self.assertFalse(store.exists("W1"))

    @mock.patch("pauk.pipeline.stages.code_links.HttpClient")
    def test_crawler_pdf_cannot_be_borrowed_as_an_explicit_arxiv_revision(self, http: mock.Mock) -> None:
        requested = "https://arxiv.org/pdf/2201.06114v2"
        other_revision = _pdf("https://github.com/org/v3-only")
        self.prepared.write_models("publications", [
            Publication(id="W1", title="Journal", doi="10.1/journal"),
            Publication(id="W2", title="Preprint", type="preprint", doi="10.48550/arXiv.2201.06114",
                        pdf_urls=[requested]),
        ])
        config = replace(self.config, pdf_crawler_url="https://crawler.example")
        http.return_value.get_bytes_with_url.side_effect = HttpRequestError("GET", requested, status_code=503)
        http.return_value.get_bytes.return_value = other_revision
        preprint_only = PreparedSelection("publications", frozenset({"W2"}))
        CodeLinksStage(self.prepared, self.raw, config=config, selection=preprint_only).run()
        store = PdfStore(self.db, self.config.pdf_dir)
        self.assertEqual(store.read_with_source("W2"), (other_revision, None))

        self._discover("2201.06114v2")
        http.reset_mock()
        http.return_value.get_bytes_with_url.side_effect = None
        http.return_value.get_bytes_with_url.return_value = (_pdf("https://github.com/org/v2-only"), requested)
        self._extract()

        http.return_value.get_bytes_with_url.assert_called_once_with(requested)
        link, = self._links().values()
        self.assertEqual(link.url, "https://github.com/org/v2-only")
        self.assertEqual(link.occurrences[0].pdf_id, requested)
        self.assertEqual(store.read("W2"), other_revision)

    @mock.patch("pauk.pipeline.stages.code_links.HttpClient")
    def test_explicit_download_rejects_a_different_final_revision(self, http: mock.Mock) -> None:
        self.prepared.write_models("publications", [Publication(id="W1", title="Journal", doi="10.1/journal")])
        source = self._discover("2201.06114v2")
        pdf = _pdf("https://github.com/org/wrong-revision")
        for final in (source.removesuffix("v2") + "v3", source.removesuffix("v2")):
            with self.subTest(final=final):
                http.return_value.get_bytes_with_url.return_value = (pdf, final)
                self._extract()
                self.assertEqual(self._source_state(source).status, ProcessingStatus.FAILED)
                self.assertEqual(self._links(), {})

    @mock.patch("pauk.pipeline.stages.code_links.HttpClient")
    def test_native_arxiv_pdf_rejects_a_redirect_to_a_different_revision(self, http: mock.Mock) -> None:
        self.prepared.write_models("publications", [Publication(
            id="W1", title="Preprint", doi="10.48550/arXiv.2201.06114v2",
        )])
        requested = "https://arxiv.org/pdf/2201.06114v2"
        http.return_value.get_bytes_with_url.return_value = (
            _pdf("https://github.com/org/wrong-revision"), "https://arxiv.org/pdf/2201.06114v3",
        )

        self._extract()

        http.return_value.get_bytes_with_url.assert_called_once_with(requested)
        self.assertEqual(self._work().processing["code_links"].status, ProcessingStatus.FAILED)
        self.assertIsNone(self._work().full_text)
        self.assertEqual(self._links(), {})
        self.assertFalse(PdfStore(self.db, self.config.pdf_dir).exists("W1"))

    @mock.patch("pauk.pipeline.stages.code_links.HttpClient")
    def test_force_reconciles_primary_evidence_after_redownload(self, http: mock.Mock) -> None:
        source = "https://arxiv.org/pdf/2201.06114"
        publisher = "https://publisher.example/paper.pdf"
        stale = CodeLink(url="https://github.com/org/stale", occurrences=[LinkOccurrence(
            pdf_id="W1", page_number=1, source="pdf_text", context="Previous primary text",
        )], is_relevant=True, classification_status=ClassificationStatus.CLASSIFIED)
        pdf = _pdf("Primary text without code")
        for via_crawler in (False, True):
            with self.subTest(via_crawler=via_crawler):
                self.prepared.write_models("publications", [Publication(
                    id="W1", title="Journal", doi="10.1/journal",
                    pdf_urls=[source] if via_crawler else [publisher, source],
                    arxiv_sources=[ArxivSource(url=source)],
                )])
                self.prepared.write_models("repo_links", [RepoLink(publication_id="W1", links=[stale])])
                config = replace(self.config, pdf_crawler_url="https://crawler.example" if via_crawler else "")
                with tempfile.TemporaryDirectory() as directory:
                    config = replace(config, data_dir=Path(directory))
                    http.return_value.get_bytes.return_value = pdf
                    http.return_value.get_bytes_with_url.side_effect = HttpRequestError("GET", source, status_code=503)
                    CodeLinksStage(self.prepared, self.raw, config=config, selection=self.selection, force=True).run()
                self.assertEqual(self._work().processing["code_links"].status, ProcessingStatus.COMPLETED_EMPTY)
                self.assertEqual(self._links(), {})

    @mock.patch("pauk.pipeline.stages.code_links.HttpClient")
    @mock.patch(STAGE_CLIENT)
    def test_native_arxiv_doi_recovers_without_lookup_or_duplicate_download(
        self, s2: mock.Mock, http: mock.Mock,
    ) -> None:
        self.prepared.write_models("publications", [Publication(
            id="W1", title="Preprint", doi="https://doi.org/10.48550/arXiv.2201.06114",
            processing={"code_links": ProcessingState(status=ProcessingStatus.COMPLETED_EMPTY)},
        )])
        source = "https://arxiv.org/pdf/2201.06114"
        actual = "https://arxiv.org/pdf/2201.06114v3"
        http.return_value.get_bytes_with_url.return_value = (_pdf("https://github.com/org/native"), actual)
        stage = ArxivVersionsStage(self.prepared, self.raw, config=self.config)
        stage.run()
        self._extract()
        stage.run()
        self._extract()

        s2.return_value.papers_by_doi.assert_not_called()
        http.return_value.get_bytes_with_url.assert_called_once_with(source)
        self.assertEqual(self._work().pdf_urls, [source])
        self.assertEqual(self._work().arxiv_sources, [])
        self.assertEqual(PdfStore(self.db, self.config.pdf_dir).read_with_source("W1")[1], actual)
        self.assertEqual(self._links()["https://github.com/org/native"].occurrences[0].pdf_id, "W1")

    @mock.patch("pauk.pipeline.stages.code_links.HttpClient")
    def test_force_reconciles_only_extracted_evidence_from_pdfs_read_successfully(self, http: mock.Mock) -> None:
        self.prepared.write_models("publications", [Publication(id="W1", title="Journal", doi="10.1/journal")])
        source = self._discover()
        unavailable = "https://arxiv.org/pdf/2201.06115"
        path = "https://github.com/org/"
        store = PdfStore(self.db, self.config.pdf_dir)
        store.save("W1", _pdf(path + "gone-primary"))
        store.save_source(source, _pdf(path + "gone-arxiv", "Old " + path + "changed", path + "same"))
        self._extract()
        previous = self._links()
        for link in previous.values():
            link.classification_status = ClassificationStatus.CLASSIFIED
            link.is_relevant = True
            link.llm_confidence = 0.9
        manual = CodeLink(url=path + "manual", is_relevant=True, classification_status=ClassificationStatus.CLASSIFIED)
        kept = CodeLink(url=path + "context", occurrences=[LinkOccurrence(pdf_id=source, context="Manual context")])
        unread = CodeLink(url=path + "unread", occurrences=[LinkOccurrence(
            pdf_id=unavailable, page_number=1, source="pdf_text", context="Prior good text",
        )])
        work = self._work()
        work.arxiv_sources.append(ArxivSource(url=unavailable))
        work.processing["link_relevance"] = ProcessingState(status=ProcessingStatus.COMPLETED)
        self.prepared.upsert_models("publications", [work])
        self.prepared.write_models("repo_links", [RepoLink(
            publication_id="W1", links=[*previous.values(), manual, kept, unread],
        )])
        store.save("W1", _pdf("No code"))
        store.save_source(source, _pdf(path + "manual", "New " + path + "changed", path + "same"))
        http.return_value.get_bytes_with_url.side_effect = HttpRequestError("GET", unavailable, status_code=503)

        self._extract(force=True)

        now = self._links()
        self.assertEqual(set(now), {path + label for label in ("changed", "same", "manual", "context", "unread")})
        self.assertEqual(now[path + "changed"].classification_status, ClassificationStatus.PENDING)
        self.assertEqual(len(now[path + "changed"].occurrences), 1)
        self.assertIn("New", now[path + "changed"].occurrences[0].context)
        self.assertEqual(now[path + "same"], previous[path + "same"])
        self.assertEqual([now[link.url] for link in (manual, kept, unread)], [manual, kept, unread])
        self.assertNotIn("link_relevance", self._work().processing)
        self.assertEqual(self._source_state(unavailable).status, ProcessingStatus.FAILED)

    @mock.patch("pauk.pipeline.stages.code_links.HttpClient")
    def test_force_primary_pdf_updates_full_text_previously_filled_by_preprint(self, http: mock.Mock) -> None:
        publisher = "https://publisher.example/paper.pdf"
        self.prepared.write_models("publications", [
            Publication(id="W1", title="Journal", doi="10.1/journal", pdf_urls=[publisher]),
        ])
        arxiv_url = self._discover()
        http.return_value.get_bytes.side_effect = HttpRequestError("GET", publisher, status_code=403)
        http.return_value.get_bytes_with_url.return_value = (
            _pdf("Preprint full text\nhttps://github.com/org/preprint"), arxiv_url,
        )
        self._extract()
        self.assertIn("Preprint full text", self._work().full_text)

        http.return_value.get_bytes.side_effect = None
        http.return_value.get_bytes.return_value = _pdf("Publisher full text\nhttps://github.com/org/publisher")
        self._extract(force=True)
        self.assertEqual(self._work().full_text.strip(), "Publisher full text\nhttps://github.com/org/publisher")

        work = self._work()
        work.arxiv_sources.append(ArxivSource(url="https://arxiv.org/pdf/2201.06115"))
        self.prepared.upsert_models("publications", [work])
        http.return_value.get_bytes_with_url.return_value = (
            _pdf("Second preprint"), "https://arxiv.org/pdf/2201.06115",
        )
        self._extract()
        self.assertEqual(self._work().full_text.strip(), "Publisher full text\nhttps://github.com/org/publisher")

    def test_merge_publication_keeps_failed_arxiv_state_over_not_started(self) -> None:
        url = "https://arxiv.org/pdf/2401.01234"
        t1 = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)
        t2 = datetime(2026, 11, 20, 11, 0, tzinfo=UTC)
        not_started = ArxivSource(url=url)
        failed_one = ArxivSource(url=url, state=ProcessingState(
            status=ProcessingStatus.FAILED, attempts=1, finished_at=t1, error="HTTP 429",
        ))
        failed_two = ArxivSource(url=url, state=ProcessingState(
            status=ProcessingStatus.FAILED, attempts=2, finished_at=t2, error="HTTP 503",
        ))
        completed = ArxivSource(url=url, state=ProcessingState(
            status=ProcessingStatus.COMPLETED, attempts=3, finished_at=t2, result_count=1,
        ))

        kept = _merge_publication(
            Publication(id="W1", title="Paper", arxiv_sources=[failed_two]),
            Publication(id="W2", title="Paper", arxiv_sources=[not_started]),
        )
        self.assertEqual(kept.arxiv_sources, [failed_two])

        promoted = _merge_publication(
            Publication(id="W1", title="Paper", arxiv_sources=[not_started]),
            Publication(id="W2", title="Paper", arxiv_sources=[failed_two]),
        )
        self.assertEqual(promoted.arxiv_sources, [failed_two])

        newer_fail = _merge_publication(
            Publication(id="W1", title="Paper", arxiv_sources=[failed_one]),
            Publication(id="W2", title="Paper", arxiv_sources=[failed_two]),
        )
        self.assertEqual(newer_fail.arxiv_sources, [failed_two])

        finished = _merge_publication(
            Publication(id="W1", title="Paper", arxiv_sources=[failed_two]),
            Publication(id="W2", title="Paper", arxiv_sources=[completed]),
        )
        self.assertEqual(finished.arxiv_sources, [completed])

    @mock.patch("pauk.pipeline.stages.code_links.HttpClient")
    def test_v2_preprint_cache_under_source_record_key_is_reused_despite_versions(self, http: mock.Mock) -> None:
        preprint_url = "https://arxiv.org/pdf/2401.01234"
        resolved_url = "https://arxiv.org/pdf/2401.01234v2"
        record = PublicationSourceRecord(
            key="openalex:W2",
            source="openalex",
            source_id="W2",
            title="Preprint",
            type="preprint",
            doi="https://doi.org/10.48550/arXiv.2401.01234",
            pdf_urls=[preprint_url],
        )
        preprint = Publication(id="pub_123", title="Preprint", schema_version=2, source_records=[record])
        self.assertEqual(len(preprint.versions), 1)
        journal = Publication(id="W1", title="Journal", doi="10.1/journal")
        self.prepared.write_models("publications", [journal, preprint])
        store = PdfStore(self.db, self.config.pdf_dir)
        store.save("openalex:W2", _pdf("https://github.com/org/v2-preprint"), source_url=resolved_url)
        self._discover("2401.01234")

        self._extract()

        http.return_value.get_bytes_with_url.assert_not_called()
        link, = self._links().values()
        self.assertEqual(link.url, "https://github.com/org/v2-preprint")
        self.assertEqual(link.occurrences[0].pdf_id, "openalex:W2")

    @mock.patch("pauk.pipeline.stages.code_links.HttpClient")
    def test_v2_uncached_preprint_saves_under_source_record_key_and_avoids_redownload(self, http: mock.Mock) -> None:
        preprint_url = "https://arxiv.org/pdf/2401.01234"
        record = PublicationSourceRecord(
            key="openalex:W2",
            source="openalex",
            source_id="W2",
            title="Preprint",
            type="preprint",
            doi="https://doi.org/10.48550/arXiv.2401.01234",
            pdf_urls=[preprint_url],
        )
        preprint = Publication(id="pub_123", title="Preprint", schema_version=2, source_records=[record])
        journal = Publication(id="W1", title="Journal", doi="10.1/journal")
        self.prepared.write_models("publications", [journal, preprint])
        self._discover("2401.01234")
        http.return_value.get_bytes_with_url.return_value = (
            _pdf("https://github.com/org/shared-v2"), "https://arxiv.org/pdf/2401.01234v1",
        )

        self._extract()
        CodeLinksStage(
            self.prepared,
            self.raw,
            config=self.config,
            selection=PreparedSelection("publications", frozenset({"pub_123"})),
        ).run()

        http.return_value.get_bytes_with_url.assert_called_once_with(preprint_url)
        store = PdfStore(self.db, self.config.pdf_dir)
        self.assertTrue(store.exists("openalex:W2"))
        self.assertFalse(store.exists("pub_123"))
        self.assertEqual(self._links()["https://github.com/org/shared-v2"].occurrences[0].pdf_id, "openalex:W2")

    @mock.patch("pauk.pipeline.stages.code_links.HttpClient")
    def test_merged_v2_preprint_with_multiple_versions_reuses_cached_source_record(self, http: mock.Mock) -> None:
        url = "https://arxiv.org/pdf/2401.01234"
        r1 = PublicationSourceRecord(
            key="openalex:W2", source="openalex", source_id="W2",
            title="Preprint", type="preprint", doi="10.48550/arXiv.2401.01234", pdf_urls=[url],
        )
        r2 = PublicationSourceRecord(
            key="openalex:W3", source="openalex", source_id="W3",
            title="Preprint", type="preprint", doi="10.48550/arXiv.2401.01234", pdf_urls=[url],
        )
        preprint = Publication(
            id="pub_merged", title="Preprint", schema_version=2,
            merged_ids=["W3"], source_records=[r1, r2],
        )
        self.assertEqual(len(preprint.versions), 2)
        journal = Publication(id="W1", title="Journal", doi="10.1/journal")
        self.prepared.write_models("publications", [journal, preprint])
        PdfStore(self.db, self.config.pdf_dir).save(
            "openalex:W2",
            _pdf("https://github.com/org/merged-preprint"),
            source_url="https://arxiv.org/pdf/2401.01234v2",
        )
        self._discover("2401.01234")

        self._extract()

        http.return_value.get_bytes_with_url.assert_not_called()
        link, = self._links().values()
        self.assertEqual(link.occurrences[0].pdf_id, "openalex:W2")

    def test_build_publication_preserves_and_resets_arxiv_sources_with_enrichment(self) -> None:
        record = PublicationSourceRecord(
            key="openalex:W1", source="openalex", source_id="W1", title="Paper", doi="10.1/a",
        )
        updated_record = record.model_copy(update={"title": "Revised Paper"})
        url = "https://arxiv.org/pdf/2401.01234"
        done = ArxivSource(url=url, state=ProcessingState(status=ProcessingStatus.COMPLETED, result_count=1))
        existing = Publication(
            id="pub_1", title="Paper", schema_version=2, doi="10.1/a",
            pdf_urls=[url], arxiv_sources=[done], source_records=[record],
        )

        kept = build_publication("pub_1", [record], existing, reset_enrichment=False)
        self.assertEqual(kept.arxiv_sources, [done])
        self.assertIn(url, kept.pdf_urls)

        unchanged = build_publication("pub_1", [record], existing, reset_enrichment=True)
        self.assertEqual(unchanged.arxiv_sources, [done])
        self.assertIn(url, unchanged.pdf_urls)

        reset = build_publication("pub_1", [updated_record], existing, reset_enrichment=True)
        self.assertEqual(reset.arxiv_sources, [])
        self.assertNotIn(url, reset.pdf_urls)


if __name__ == "__main__":
    unittest.main()
