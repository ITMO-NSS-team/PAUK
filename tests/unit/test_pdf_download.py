import gzip
import os
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from collections import Counter
from concurrent.futures import CancelledError, ThreadPoolExecutor
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, quote, urlsplit

import mongomock
import pymupdf
import requests

from pauk.models import Publication, RepoLink
from pauk.models.processing import ProcessingStatus
from pauk.pipeline.stages.code_links import CodeLinksStage, _extract_pdf
from pauk.settings import Settings
from pauk.sources.base import HttpClient, HttpRequestError
from pauk.sources.pdf import PdfDownloader
from pauk.sources.rate_limit import HostRateLimiter, RateLimit
from pauk.storage import PreparedStore, RawStore

_SEND = requests.Session.send


class _LocalSession(requests.Session):
    def send(self, request, **kwargs):
        # Benchmark fixtures replace Session.send; this bypass permits only our local server.
        if urlsplit(request.url).hostname not in {"127.0.0.1", "localhost"}:
            raise AssertionError("test attempted an external HTTP request")
        return _SEND(self, request, **kwargs)


def _client(*args, **kwargs):
    client = HttpClient(*args, **kwargs)
    client.session.close()
    client.session = _LocalSession()
    return client


def _pdf():
    with pymupdf.open() as document:
        document.new_page().insert_text((72, 72), "Code: https://github.com/org/repo")
        return document.tobytes()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def handle(self):
        with suppress(ConnectionResetError):
            super().handle()

    def do_GET(self):
        host = urlsplit("http://" + self.headers["Host"]).hostname
        path = urlsplit(self.path).path
        with self.server.lock:
            self.server.active[host] += 1
            self.server.peak[host] = max(self.server.peak[host], self.server.active[host])
            self.server.counts[path] += 1
            count = self.server.counts[path]
        try:
            self.respond(path, count)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
            self.close_connection = True
        finally:
            with self.server.lock:
                self.server.active[host] -= 1

    def respond(self, path, count):
        query = parse_qs(urlsplit(self.path).query)
        if path == "/headers":
            time.sleep(float(query.get("delay", [0.4])[0]))
        if path == "/redirect-slow":
            time.sleep(float(query.get("delay", [0.1])[0]))
        body = self.server.pdf
        status = 200
        headers = {}
        if path == "/html":
            body = b"<html>not a PDF</html>"
        elif path == "/refused":
            status = 403
        elif (path == "/flaky" and count == 1) or path == "/unreachable":
            status = 503
        elif path == "/pause":
            status = 429
            headers["Retry-After"] = "3600"
        elif path in {"/redirect", "/redirect-drip", "/redirect-slow"}:
            status = 302
            body = b"redirect"
            headers["Location"] = query.get("to", ["/pdf"])[0]
        elif path == "/loop":
            status = 302
            body = b""
            headers["Location"] = "/loop"
        chunked = path.startswith("/chunked")
        if path == "/chunked-gzip":
            body = gzip.compress(body)
            headers["Content-Encoding"] = "gzip"
        self.send_response(status)
        self.send_header("Content-Type", "text/html" if path == "/html" else "application/pdf")
        self.send_header("Transfer-Encoding" if chunked else "Content-Length", "chunked" if chunked else str(len(body)))
        for name, value in headers.items():
            self.send_header(name, value)
        if query.get("close"):
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.flush()
        if path == "/held":
            self.server.held.set()
            self.server.release.wait(2)
        if path == "/stall":
            time.sleep(0.4)
        if path == "/drop" and count == 1:
            self.wfile.write(body[:12])
            self.wfile.flush()
            self.close_connection = True
            return
        if chunked:
            for part in (body[:23], body[23:]):
                self.wfile.write(f"{len(part):X}\r\n".encode())
                if path == "/chunked-drop" and count == 1:
                    self.wfile.write(part[:3])
                    self.wfile.flush()
                    self.close_connection = True
                    return
                self.wfile.write(part + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
        elif path in {"/drip", "/redirect-drip"}:
            for byte in body:
                self.wfile.write(bytes([byte]))
                self.wfile.flush()
                time.sleep(0.02)
        else:
            self.wfile.write(body)
        self.wfile.flush()


class LocalPdfTest(unittest.TestCase):
    secure = False

    @classmethod
    def setUpClass(cls):
        if cls.secure and shutil.which("openssl") is None:
            raise unittest.SkipTest("openssl is needed for the local HTTPS fixture")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.server.daemon_threads = True
        cls.server.lock = threading.Lock()
        cls.server.active, cls.server.peak, cls.server.counts = Counter(), Counter(), Counter()
        cls.server.held, cls.server.release = threading.Event(), threading.Event()
        cls.server.pdf = _pdf()
        if cls.secure:
            temporary = tempfile.TemporaryDirectory()
            cls.addClassCleanup(temporary.cleanup)
            root = Path(temporary.name)
            config = root / "cert.cnf"
            config.write_text("[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n"
                              "[dn]\nCN=localhost\n[ext]\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n")
            cls.cert = root / "cert.pem"
            key = root / "key.pem"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                            "-keyout", str(key), "-out", str(cls.cert), "-config", str(config)],
                           check=True, capture_output=True)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cls.cert, key)
            cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"{'https' if cls.secure else 'http'}://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.release.set()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}))
        self.enterContext(patch("pauk.sources.base.RATE_LIMITER", HostRateLimiter()))
        with self.server.lock:
            self.server.counts.clear()
            self.server.peak.clear()
        self.server.held.clear()
        self.server.release.clear()
        self.addCleanup(self.server.release.set)
        self.client = _client(0.2, default_rate_limit=RateLimit(max_concurrent=2))
        if self.secure:
            self.client.session.verify = str(self.cert)
        self.addCleanup(self.client.close)

    def fetch(self, path, **kwargs):
        return self.client.get_bytes(self.url + path, retries=kwargs.pop("retries", 0),
                                     deadline=kwargs.pop("deadline", 0.5), **kwargs)


class PdfTransportTest(LocalPdfTest):
    def test_retry_count_is_applied_per_pdf_without_changing_generic_api_defaults(self):
        for retries in (0, 1, 2, 3):
            with self.subTest(retries=retries), patch("pauk.sources.base.HttpClient._backoff_delay", return_value=0):
                before = self.server.counts["/unreachable"]
                downloader = PdfDownloader(Settings(pdf_retries=retries), client_factory=_client)
                try:
                    with self.assertRaises(HttpRequestError):
                        downloader.get_bytes(self.url + "/unreachable")
                    self.assertEqual(self.server.counts["/unreachable"] - before, retries + 1)
                finally:
                    downloader.close()

    def test_normal_chunked_and_gzip_bodies_are_complete(self):
        for path in ("/pdf", "/chunked", "/chunked-gzip"):
            with self.subTest(path=path):
                self.assertEqual(self.fetch(path), self.server.pdf)
        self.assertFalse(self.client._responses)

    def test_header_status_and_partial_body_failures_retry_the_entire_request(self):
        for path in ("/flaky", "/drop", "/chunked-drop"):
            with self.subTest(path=path), patch("pauk.sources.base.HttpClient._backoff_delay", return_value=0):
                self.assertEqual(self.fetch(path, retries=1), self.server.pdf)
                self.assertEqual(self.server.counts[path], 2)

    def test_403_is_not_retried(self):
        with self.assertRaises(HttpRequestError) as context:
            self.fetch("/refused", retries=3)
        self.assertEqual(context.exception.status_code, 403)
        self.assertEqual(self.server.counts["/refused"], 1)

    def test_deadline_covers_headers_body_and_redirect_body(self):
        for path in ("/headers", "/drip", "/drip?close=1", "/redirect-drip"):
            with self.subTest(path=path):
                started = time.monotonic()
                with self.assertRaises(HttpRequestError):
                    self.fetch(path, timeout=1, deadline=0.15)
                self.assertLess(time.monotonic() - started, 0.8)
                self.assertFalse(self.client._responses)
                self.assertEqual(self.fetch("/pdf"), self.server.pdf)

    def test_read_timeout_is_independent_of_the_whole_attempt_budget(self):
        started = time.monotonic()
        with self.assertRaises(HttpRequestError):
            self.fetch("/stall", timeout=(0.1, 0.06), deadline=1)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_zero_wait_budget_does_not_sleep_or_retry_after_a_network_timeout(self):
        with self.assertRaises(HttpRequestError):
            self.fetch("/stall", timeout=(0.1, 0.03), retries=3, max_wait=0)
        self.assertEqual(self.server.counts["/stall"], 1)

    def test_redirect_hops_share_one_deadline(self):
        target = quote(self.url + "/headers?delay=0.1", safe="")
        started = time.monotonic()
        with self.assertRaises(HttpRequestError):
            self.fetch("/redirect-slow?delay=0.1&to=" + target, deadline=0.15, timeout=1)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertFalse(self.client._responses)

    def test_redirects_are_bounded(self):
        with self.assertRaises(HttpRequestError):
            self.fetch("/loop", timeout=0.2, deadline=1)

    def test_final_429_preserves_shared_pause_without_waiting_an_hour(self):
        with self.assertRaises(HttpRequestError):
            self.fetch("/pause", retries=0, max_wait=1)
        second = _client(0.1, default_rate_limit=RateLimit(max_concurrent=2))
        self.addCleanup(second.close)
        with self.assertRaisesRegex(HttpRequestError, "host paused"):
            second.get_bytes(self.url + "/pdf", retries=3, deadline=0.3, max_wait=1)
        self.assertEqual(self.server.counts["/pdf"], 0)

    def test_cancelling_a_slow_body_releases_the_worker_and_host_slot(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.fetch, "/drip", timeout=1, deadline=5)
            until = time.monotonic() + 1
            while not self.client._responses and time.monotonic() < until:
                time.sleep(0.005)
            self.assertTrue(self.client._responses)
            self.client.cancel()
            with self.assertRaises(CancelledError):
                future.result(timeout=1)
        self.assertFalse(self.client._responses)

    def test_redirect_target_concurrency_is_shared_between_clients(self):
        config = Settings(pdf_host_concurrency=1, pdf_retries=0, pdf_read_timeout=1)
        downloader = PdfDownloader(config, client_factory=_client)
        self.addCleanup(downloader.close)
        target = f"http://localhost:{self.server.server_port}/headers?delay=0.06"
        urls = [target, self.url + "/redirect?to=" + target, target]
        with ThreadPoolExecutor(max_workers=3) as pool:
            result = list(pool.map(downloader.get_bytes, urls))
        self.assertTrue(all(data == self.server.pdf for data in result))
        self.assertEqual(self.server.peak["localhost"], 1)
        self.assertGreaterEqual(len(downloader._clients), 2)
        self.assertEqual(len({id(client.session) for client in downloader._clients}), len(downloader._clients))


class PdfStageTest(LocalPdfTest):
    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = mongomock.MongoClient()["pauk_test"]
        self.prepared = PreparedStore(self.db, "test")
        self.raw = RawStore(self.db, "test")
        self.config = Settings(data_dir=Path(folder.name), pdf_read_timeout=0.5,
                               pdf_connect_timeout=0.2, pdf_download_deadline=1, pdf_retries=0)
        self.enterContext(patch("pauk.pipeline.stages.code_links.HttpClient", side_effect=_client))

    def test_fast_results_are_saved_and_replaced_before_a_slow_first_pdf_finishes(self):
        self.prepared.write_models("publications", [
            Publication(id="W1", title="slow", pdf_urls=[self.url + "/held"]),
            Publication(id="W2", title="fast", pdf_urls=[self.url + "/pdf"]),
            Publication(id="W3", title="next", pdf_urls=[self.url + "/pdf"]),
        ])
        seen = []

        def progress(_label, done, _total):
            if done == 2:
                rows = {row.id: row for row in self.prepared.read_models("publications", Publication)}
                self.assertIn("code_links", rows["W2"].processing)
                self.assertIn("code_links", rows["W3"].processing)
                self.assertNotIn("code_links", rows["W1"].processing)
                seen.append(done)
                self.server.release.set()

        config = Settings(**(self.config.__dict__ | {"code_links_concurrency": 2, "pdf_host_concurrency": 2}))
        stage = CodeLinksStage(self.prepared, self.raw, config=config, on_progress=progress)
        stage.REPORT_SECONDS = 0
        original = _extract_pdf

        def extract(data):
            self.assertIs(threading.current_thread(), threading.main_thread())
            return original(data)

        with patch("pauk.pipeline.stages.code_links._extract_pdf", side_effect=extract):
            self.assertEqual(stage.run()["publications"], 3)
        self.assertTrue(seen)
        self.assertTrue(all(row.processing["code_links"].status == ProcessingStatus.COMPLETED
                            for row in self.prepared.read_models("publications", Publication)))

    def test_one_host_does_not_fill_the_pool_while_other_hosts_are_ready(self):
        other = self.url.replace("127.0.0.1", "localhost")
        self.prepared.write_models("publications", [
            Publication(id="W1", title="slow", pdf_urls=[self.url + "/held"]),
            Publication(id="W2", title="same host", pdf_urls=[self.url + "/pdf"]),
            Publication(id="W3", title="other host", pdf_urls=[other + "/pdf"]),
        ])

        def progress(_label, done, _total):
            if done:
                self.assertIsNotNone(self.db.publications.find_one({"_id": "W3", "_processing.code_links.status": "completed"}))
                self.server.release.set()

        config = Settings(**(self.config.__dict__ | {"code_links_concurrency": 2, "pdf_host_concurrency": 1}))
        stage = CodeLinksStage(self.prepared, self.raw, config=config, on_progress=progress)
        stage.REPORT_SECONDS = 0
        self.assertEqual(stage.run()["publications"], 3)

    def test_cancellation_retains_completed_rows_and_does_not_mark_pending_rows_failed(self):
        self.prepared.write_models("publications", [
            Publication(id="W1", title="slow", pdf_urls=[self.url + "/drip"]),
            Publication(id="W2", title="fast", pdf_urls=[self.url + "/pdf"]),
        ])

        def progress(_label, done, _total):
            if done:
                raise RuntimeError("stop requested")

        stage = CodeLinksStage(self.prepared, self.raw, config=self.config, on_progress=progress)
        stage.REPORT_SECONDS = 0
        with self.assertRaisesRegex(RuntimeError, "stop requested"):
            stage.run()
        rows = {row.id: row for row in self.prepared.read_models("publications", Publication)}
        self.assertNotIn("code_links", rows["W1"].processing)
        self.assertEqual(rows["W2"].processing["code_links"].status, ProcessingStatus.COMPLETED)
        self.assertEqual(len(list(self.prepared.read_models("repo_links", RepoLink))), 1)
        self.assertTrue(all(client._cancelled.is_set() and not client._responses for client in stage.http._clients))
        rows["W1"].pdf_urls = [self.url + "/pdf"]
        self.prepared.upsert_models("publications", [rows["W1"]])
        resumed = CodeLinksStage(self.prepared, self.raw, config=self.config).run()
        self.assertEqual(resumed["publications"], 1)
        saved = {row.id: row for row in self.prepared.read_models("publications", Publication)}
        self.assertEqual(set(saved), {"W1", "W2"})
        self.assertEqual(saved["W2"].processing["code_links"].attempts, 1)

    def test_html_falls_back_without_poisoning_cache_and_cached_pdf_avoids_the_network(self):
        self.prepared.write_models("publications", [Publication(
            id="W1", title="paper", pdf_urls=[self.url + "/html", self.url + "/chunked-gzip"])])
        stage = CodeLinksStage(self.prepared, self.raw, config=self.config)
        stage.run()
        self.assertEqual((self.config.pdf_dir / "W1.pdf").read_bytes(), self.server.pdf)
        calls = dict(self.server.counts)
        CodeLinksStage(self.prepared, self.raw, config=self.config, force=True).run()
        self.assertEqual(dict(self.server.counts), calls)

    def test_crawler_is_probed_once_and_used_after_all_direct_sources_fail(self):
        self.prepared.write_models("publications", [
            Publication(id="W1", title="paper", doi="https://doi.org/10.1/test",
                        pdf_urls=[self.url + "/refused", self.url + "/html"]),
            Publication(id="W2", title="without direct sources", doi="https://doi.org/10.2/test"),
        ])
        config = Settings(**(self.config.__dict__ | {"pdf_crawler_url": self.url + "/api"}))
        CodeLinksStage(self.prepared, self.raw, config=config).run()
        self.assertEqual(self.server.counts["/api/health"], 1)
        self.assertEqual(self.server.counts["/api/download"], 2)
        for publication in self.prepared.read_models("publications", Publication):
            self.assertEqual(publication.processing["code_links"].status, ProcessingStatus.COMPLETED)
            self.assertEqual((config.pdf_dir / f"{publication.id}.pdf").read_bytes(), self.server.pdf)

    def test_cached_versions_are_isolated_between_parallel_publications(self):
        self.config.pdf_dir.mkdir(parents=True)
        for identifier in ("V1", "V2"):
            (self.config.pdf_dir / f"{identifier}.pdf").write_bytes(self.server.pdf)
        self.prepared.write_models("publications", [
            Publication(id="W1", title="first", merged_ids=["V1"]),
            Publication(id="W2", title="second", merged_ids=["V2"]),
        ])
        CodeLinksStage(self.prepared, self.raw, config=self.config).run()
        for publication in self.prepared.read_models("publications", Publication):
            self.assertEqual(publication.processing["code_links"].status, ProcessingStatus.COMPLETED)
        links = {row.publication_id: row for row in self.prepared.read_models("repo_links", RepoLink)}
        for pub_id, pdf_id in (("W1", "V1"), ("W2", "V2")):
            self.assertEqual({occ.pdf_id for link in links[pub_id].links for occ in link.occurrences}, {pdf_id})
        self.assertFalse(self.server.counts)


class HttpsPdfTest(LocalPdfTest):
    secure = True

    def test_deadline_interrupts_https_for_close_and_keep_alive_responses(self):
        for suffix in ("", "?close=1"):
            with self.subTest(suffix=suffix):
                with self.assertRaises(HttpRequestError):
                    self.fetch("/drip" + suffix, timeout=1, deadline=0.15)
                self.assertFalse(self.client._responses)
                self.assertEqual(self.fetch("/pdf"), self.server.pdf)

    def test_cancellation_before_headers_finishes_within_the_socket_timeout(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.fetch, "/headers", timeout=0.1, deadline=1)
            until = time.monotonic() + 1
            while not self.server.counts["/headers"] and time.monotonic() < until:
                time.sleep(0.005)
            self.assertTrue(self.server.counts["/headers"])
            self.client.cancel()
            with self.assertRaises(CancelledError):
                future.result(timeout=0.8)


class PdfParameterTest(unittest.TestCase):
    def test_invalid_stream_parameters_are_rejected_before_a_request(self):
        cases = (
            ("deadline", (0, -1, True, float("nan"), float("inf"))),
            ("max_wait", (-1, True, float("nan"), float("inf"))),
            ("retries", (-1, True, 0.5)),
        )
        client = _client(1)
        self.addCleanup(client.close)
        with patch.object(client, "_request") as request:
            for name, values in cases:
                for value in values:
                    with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                        client.get_bytes("https://pdf.invalid/file", **({"deadline": 1} | {name: value}))
            request.assert_not_called()


class PdfSettingsTest(unittest.TestCase):
    def test_pdf_retry_limit_is_configurable_including_zero_and_three(self):
        for retries in (0, 1, 2, 3):
            with self.subTest(retries=retries):
                self.assertEqual(Settings(pdf_retries=retries).pdf_retries, retries)

    def test_invalid_pdf_limits_fail_before_a_stage_starts(self):
        for name in ("code_links_concurrency", "pdf_host_concurrency", "pdf_connect_timeout",
                     "pdf_read_timeout", "pdf_download_deadline"):
            for value in (0, -1, True):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    Settings(**{name: value})
        for name in ("pdf_connect_timeout", "pdf_read_timeout", "pdf_download_deadline", "pdf_max_retry_wait"):
            for value in (float("nan"), float("inf")):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    Settings(**{name: value})
        self.assertEqual(Settings(pdf_retries=0, pdf_max_retry_wait=0).pdf_retries, 0)
