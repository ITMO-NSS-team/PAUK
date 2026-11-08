import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fitz
import mongomock

from pauk.models import Publication, RepoLink
from pauk.models.processing import ProcessingState, ProcessingStatus
from pauk.pipeline.stages import ALL_STAGES
from pauk.pipeline.stages.arxiv_versions import (
    ArxivVersionsStage,
    bare_doi,
    has_arxiv_link,
)
from pauk.pipeline.stages.code_links import CodeLinksStage
from pauk.settings import ARXIV_RPS, Settings
from pauk.sources.base import HttpClient, HttpRequestError
from pauk.sources.semantic_scholar import (
    BATCH_URL,
    MAX_BATCH,
    SemanticScholarClient,
    arxiv_id,
)
from pauk.storage import PreparedStore, RawStore

STAGE_CLIENT = "pauk.pipeline.stages.arxiv_versions.SemanticScholarClient"


def _pdf(text: str) -> bytes:
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), text)
    try:
        return doc.tobytes()
    finally:
        doc.close()


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
        limit = HttpClient(timeout=5).rate_limits["arxiv.org"]
        self.assertEqual(limit.requests_per_second, ARXIV_RPS)
        self.assertEqual(limit.max_concurrent, 1)


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
    @mock.patch(STAGE_CLIENT)
    def test_code_links_reads_the_preprint_when_the_publisher_refuses(self, s2, http):
        publisher = "https://publisher.example/paper.pdf"
        self.prepared.write_models("publications", [Publication(
            id="W1", title="t", doi="10.1/a", pdf_urls=[publisher],
            processing={"code_links": ProcessingState(status=ProcessingStatus.FAILED)},
        )])
        s2.return_value.papers_by_doi.return_value = [_record("2201.06114")]

        def download(url, **_):
            if url == publisher:
                raise HttpRequestError("GET", url, status_code=403)
            return _pdf("Our code: https://github.com/org/repo")

        http.return_value.get_bytes.side_effect = download
        ArxivVersionsStage(self.prepared, self.raw, config=self.config).run()
        CodeLinksStage(self.prepared, self.raw, config=self.config).run()
        pub = next(iter(self.prepared.read_models("publications", Publication)))
        self.assertIn("github.com/org/repo", pub.full_text)
        self.assertEqual(pub.processing["code_links"].status, ProcessingStatus.COMPLETED)
        links = next(iter(self.prepared.read_models("repo_links", RepoLink)))
        self.assertEqual([link.url for link in links.links], ["https://github.com/org/repo"])
        self.assertEqual([call.args[0] for call in http.return_value.get_bytes.call_args_list],
                         [publisher, "https://arxiv.org/pdf/2201.06114"])


if __name__ == "__main__":
    unittest.main()
