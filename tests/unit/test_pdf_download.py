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
from pymongo.errors import AutoReconnect

from pauk.models import CodeLink, Publication, RepoLink
from pauk.models.processing import ProcessingState, ProcessingStatus
from pauk.pipeline.stages.code_links import CodeLinksStage, _extract_pdf
from pauk.settings import Settings
from pauk.sources.base import DownloadState, HttpClient, HttpRequestError
from pauk.sources.pdf import PdfDownloader
from pauk.sources.rate_limit import HostRateLimiter, RateLimit, RequestDeferred
from pauk.storage import PdfStore, PreparedStore, RawStore
from tests.unit.test_rate_limit import _Clock

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
        if path == "/headers-drip":
            self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Drip: ")
            for _ in range(30):
                self.wfile.write(b"x")
                self.wfile.flush()
                time.sleep(0.035)
            self.wfile.write(b"\r\nContent-Length: 0\r\n\r\n")
            return
        if path == "/headers":
            time.sleep(float(query.get("delay", [0.4])[0]))
        if path == "/redirect-slow":
            time.sleep(float(query.get("delay", [0.1])[0]))
        if path == "/after-cache":
            self.server.held.wait(2)
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
        elif path == "/pause-once" and count == 1:
            status = 429
            headers["Retry-After"] = "1"
        elif path == "/limited":
            headers.update({"X-Concurrency-Limit": "1", "X-Rate-Limit-Limit": "2", "X-Rate-Limit-Interval": "1s"})
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
        if path == "/held" or (path == "/api/download" and query.get("url") == ["https://doi.org/10.1/held"]):
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
        self.enterContext(patch("pauk.sources.pdf.PDF_RATE_LIMITER", HostRateLimiter()))
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
    def test_deferred_api_admission_does_not_reserve_a_pdf_start(self):
        clock = _Clock()
        host = urlsplit(self.url).hostname
        api = HostRateLimiter(clock=clock)
        pdf = HostRateLimiter(clock=clock)
        client = _client(0.2, rate_limiter=api, rate_limits={host: RateLimit(5)},
                         default_rate_limiter=pdf, default_rate_limit=RateLimit(10, 2))
        self.addCleanup(client.close)
        self.assertEqual(client.get_bytes(self.url + "/pdf", state=DownloadState()), self.server.pdf)
        clock.now += 0.15
        state = DownloadState()
        with self.assertRaises(RequestDeferred) as deferred:
            client.get_bytes(self.url + "/pdf", state=state)
        self.assertAlmostEqual(deferred.exception.ready_at, 100.2)
        self.assertEqual(self.server.counts["/pdf"], 1)
        clock.now = deferred.exception.ready_at
        self.assertEqual(client.get_bytes(self.url + "/pdf", state=state), self.server.pdf)

    def test_synchronous_download_deadline_also_starts_after_admission(self):
        limiter = self.client.rate_limiter
        with limiter.slot("127.0.0.1", RateLimit()):
            limiter.pause("127.0.0.1", 0.4)
        self.assertEqual(self.fetch("/pdf", deadline=0.2, max_wait=1), self.server.pdf)

    def test_pdf_concurrency_does_not_lower_an_api_profile_on_the_same_host(self):
        host = urlsplit(self.url).hostname
        pdf_limiter = HostRateLimiter(host_limits={host: RateLimit(10)})
        self.enterContext(patch("pauk.sources.pdf.PDF_RATE_LIMITER", pdf_limiter))
        profile = {host: RateLimit(100, 5)}
        api = _client(1, rate_limits=profile)
        self.addCleanup(api.close)
        api.get_bytes(self.url + "/pdf", retries=0)

        def factory(*args, **kwargs):
            return _client(*args, rate_limits=profile, **kwargs)

        downloader = PdfDownloader(Settings(pdf_host_concurrency=2, pdf_retries=0), client_factory=factory)
        try:
            with ThreadPoolExecutor(max_workers=3) as pool:
                held = [pool.submit(downloader.get_bytes, self.url + "/held") for _ in range(2)]
                try:
                    until = time.monotonic() + 1
                    while self.server.counts["/held"] < 2 and time.monotonic() < until:
                        time.sleep(0.005)
                    self.assertEqual(self.server.counts["/held"], 2)
                    ordinary = pool.submit(api.get_bytes, self.url + "/pdf", retries=0)
                    self.assertEqual(ordinary.result(timeout=0.4), self.server.pdf)
                finally:
                    self.server.release.set()
                self.assertTrue(all(future.result(timeout=1) == self.server.pdf for future in held))
        finally:
            downloader.close()
        self.assertEqual(api.rate_limiter._hosts[host].limit, RateLimit(100, 5))
        self.assertEqual(pdf_limiter._hosts[host].limit, RateLimit(10))

    def test_pdf_server_headers_apply_to_a_later_api_client(self):
        downloader = PdfDownloader(Settings(pdf_retries=0), client_factory=_client)
        self.addCleanup(downloader.close)
        self.assertEqual(downloader.get_bytes(self.url + "/limited"), self.server.pdf)
        downloader.close()
        host = urlsplit(self.url).hostname
        api = _client(1, rate_limits={host: RateLimit(100, 5)})
        self.addCleanup(api.close)
        api.get_bytes(self.url + "/pdf", retries=0)
        self.assertEqual(api.rate_limiter._hosts[host].limit, RateLimit(2, 1))

    def test_downloaders_share_the_strictest_active_host_cap(self):
        strict = PdfDownloader(Settings(pdf_host_concurrency=1, pdf_retries=0), client_factory=_client)
        relaxed = PdfDownloader(Settings(pdf_host_concurrency=2, pdf_retries=0), client_factory=_client)
        self.addCleanup(strict.close)
        self.addCleanup(relaxed.close)
        self.assertEqual(strict.get_bytes(self.url + "/pdf"), self.server.pdf)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(relaxed.get_bytes, self.url + "/held")
            try:
                self.assertTrue(self.server.held.wait(1))
                with self.assertRaises(RequestDeferred):
                    relaxed.get_bytes(self.url + "/pdf", state=DownloadState())
                self.assertEqual(self.server.counts["/pdf"], 1)
                strict.close()
                second = pool.submit(relaxed.get_bytes, self.url + "/held")
                until = time.monotonic() + 1
                while self.server.counts["/held"] < 2 and time.monotonic() < until:
                    time.sleep(0.005)
                self.assertEqual(self.server.counts["/held"], 2)
                with self.assertRaises(RequestDeferred):
                    relaxed.get_bytes(self.url + "/pdf", state=DownloadState())
            finally:
                self.server.release.set()
            self.assertEqual(first.result(timeout=1), self.server.pdf)
            self.assertEqual(second.result(timeout=1), self.server.pdf)

    def test_pdf_responses_keep_server_pauses_shared_with_api_clients(self):
        host = urlsplit(self.url).hostname
        profile = {host: RateLimit(100, 5)}
        api = _client(1, rate_limits=profile)
        self.addCleanup(api.close)
        api.get_bytes(self.url + "/pdf", retries=0)

        def factory(*args, **kwargs):
            return _client(*args, rate_limits=profile, **kwargs)

        downloader = PdfDownloader(Settings(pdf_retries=0, pdf_max_retry_wait=0), client_factory=factory)
        self.addCleanup(downloader.close)
        with self.assertRaises(HttpRequestError):
            downloader.get_bytes(self.url + "/pause")
        self.assertGreater(api.rate_limiter._hosts[host].paused_until, time.monotonic() + 3590)

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
        self.assertFalse(self.client._active_reads)

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
        for path in ("/headers", "/headers-drip", "/drip", "/drip?close=1", "/redirect-drip"):
            with self.subTest(path=path):
                started = time.monotonic()
                with self.assertRaises(HttpRequestError):
                    self.fetch(path, timeout=1, deadline=0.15)
                self.assertLess(time.monotonic() - started, 0.65)
                self.assertFalse(self.client._active_reads)
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
        self.assertFalse(self.client._active_reads)

    def test_redirects_are_bounded(self):
        with self.assertRaises(HttpRequestError):
            self.fetch("/loop", timeout=0.2, deadline=1)

    def test_streamed_download_returns_the_final_url_after_deferred_redirect(self):
        target = self.url.replace("127.0.0.1", "localhost") + "/pdf"
        source = self.url + "/redirect?to=" + quote(target, safe="")
        state = DownloadState()
        with (self.client.rate_limiter.slot("localhost", RateLimit(max_concurrent=1)),
              self.assertRaises(RequestDeferred)):
            self.client.get_bytes_with_url(source, deadline=1, retries=0, state=state)

        data, final_url = self.client.get_bytes_with_url(source, deadline=1, retries=0, state=state)

        self.assertEqual(data, self.server.pdf)
        self.assertEqual(final_url, target)
        self.assertEqual(self.server.counts["/redirect"], 1)

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
            while not self.client._active_reads and time.monotonic() < until:
                time.sleep(0.005)
            self.assertTrue(self.client._active_reads)
            self.client.cancel()
            with self.assertRaises(CancelledError):
                future.result(timeout=1)
        self.assertFalse(self.client._active_reads)

    def test_redirect_target_concurrency_is_shared_between_clients(self):
        pdf_limiter = HostRateLimiter(host_limits={"localhost": RateLimit(20)})
        self.enterContext(patch("pauk.sources.pdf.PDF_RATE_LIMITER", pdf_limiter))
        config = Settings(pdf_host_concurrency=1, pdf_retries=0, pdf_read_timeout=1)
        downloader = PdfDownloader(config, client_factory=_client)
        self.addCleanup(downloader.close)
        target = f"http://localhost:{self.server.server_port}/headers?delay=0.06"
        urls = [target, self.url + "/redirect?to=" + target, target]
        with ThreadPoolExecutor(max_workers=3) as pool:
            result = list(pool.map(downloader.get_bytes, urls))
        self.assertTrue(all(data == self.server.pdf for data in result))
        self.assertEqual(self.server.peak["localhost"], 1)
        self.assertEqual(pdf_limiter._hosts["localhost"].limit, RateLimit(20))
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

    def test_host_pause_does_not_delay_cached_publications_from_the_same_host(self):
        self.prepared.write_models("publications", [
            Publication(id=key, title="paper", pdf_urls=[self.url + "/pdf"]) for key in ("W1", "W2")
        ])
        PdfStore(self.db, self.config.pdf_dir).save("W2", self.server.pdf)
        limiter = self.client.rate_limiter
        with limiter.slot("127.0.0.1", RateLimit()):
            limiter.pause("127.0.0.1", 1)

        def stop_after_cached(_label, done, _total):
            if done:
                raise CancelledError

        config = Settings(**(self.config.__dict__ | {"pdf_host_concurrency": 1}))
        stage = CodeLinksStage(self.prepared, self.raw, config=config, on_progress=stop_after_cached)
        stage.REPORT_SECONDS = 0
        with self.assertRaises(CancelledError):
            stage.run()
        rows = {publication.id: publication for publication in self.prepared.read_models("publications", Publication)}
        self.assertNotIn("code_links", rows["W1"].processing)
        self.assertEqual(rows["W2"].processing["code_links"].status, ProcessingStatus.COMPLETED)
        self.assertEqual(self.server.counts["/pdf"], 0)

    def test_waiting_for_admission_does_not_spend_the_attempt_deadline(self):
        records = [Publication(id=f"W{i}", title="paper", pdf_urls=[self.url + "/pdf"]) for i in range(8)]
        self.prepared.write_models("publications", records)
        limiter = self.client.rate_limiter
        with limiter.slot("127.0.0.1", RateLimit()):
            limiter.pause("127.0.0.1", 0.6)
        config = Settings(**(self.config.__dict__ | {"pdf_host_concurrency": 1, "pdf_download_deadline": 0.3}))
        calls = []
        original = HttpClient.get_bytes

        def fetch(client, url, **kwargs):
            calls.append(url)
            return original(client, url, **kwargs)

        with patch.object(HttpClient, "get_bytes", fetch):
            CodeLinksStage(self.prepared, self.raw, config=config).run()
        self.assertEqual(len(calls), len(records) + 1)
        self.assertEqual(self.server.counts["/pdf"], len(records))
        for publication in self.prepared.read_models("publications", Publication):
            self.assertEqual(publication.processing["code_links"].status, ProcessingStatus.COMPLETED)

    def test_network_starts_before_all_publication_caches_are_checked(self):
        records = [Publication(id=f"W{i}", title="paper", pdf_urls=[self.url + "/held"]) for i in range(2)]
        self.prepared.write_models("publications", records)
        store = PdfStore(self.db, self.config.pdf_dir)
        find = store.pointers.find_one

        def find_one(query, *args, **kwargs):
            if query.get("_id") == "W1":
                self.assertTrue(self.server.held.wait(1))
                self.server.release.set()
            return find(query, *args, **kwargs)

        with patch.object(store.pointers, "find_one", side_effect=find_one):
            CodeLinksStage(self.prepared, self.raw, config=self.config).run()
        self.assertEqual(self.server.counts["/held"], len(records))

    def test_a_paused_redirect_target_can_exhaust_the_started_attempt(self):
        target = self.url.replace("127.0.0.1", "localhost") + "/redirect-target"
        with self.client.rate_limiter.slot("localhost", RateLimit()):
            self.client.rate_limiter.pause("localhost", 2)
        self.prepared.write_models("publications", [
            Publication(id="W1", title="paper", pdf_urls=[self.url + "/redirect?to=" + quote(target, safe="")])
        ])
        config = Settings(**(self.config.__dict__ | {"pdf_download_deadline": 0.3}))
        CodeLinksStage(self.prepared, self.raw, config=config).run()
        saved = next(self.prepared.read_models("publications", Publication))
        self.assertIn("deadline", saved.processing["code_links"].error)
        self.assertEqual(self.server.counts["/redirect"], 1)
        self.assertEqual(self.server.counts["/redirect-target"], 0)

    def _check_fallback_releases_the_previous_host(self, *, crawler):
        other = self.url.replace("127.0.0.1", "localhost")
        urls = [self.url + "/refused"] if crawler else [self.url + "/refused", other + "/held"]
        self.prepared.write_models("publications", [
            Publication(id="W1", title="fallback", pdf_urls=urls,
                        doi="https://doi.org/10.1/held" if crawler else None),
            Publication(id="W2", title="same initial host", pdf_urls=[self.url + "/pdf"]),
        ])
        observed = []

        def progress(_label, done, _total):
            if done and not observed:
                rows = {row.id: row for row in self.prepared.read_models("publications", Publication)}
                self.assertIn("code_links", rows["W2"].processing)
                self.assertEqual(rows["W2"].processing["code_links"].status, ProcessingStatus.COMPLETED)
                self.assertNotIn("code_links", rows["W1"].processing)
                observed.append(done)
                self.server.release.set()

        config = Settings(**(self.config.__dict__ | {
            "code_links_concurrency": 2, "pdf_host_concurrency": 1,
            "pdf_crawler_url": other + "/api" if crawler else "",
        }))
        stage = CodeLinksStage(self.prepared, self.raw, config=config, on_progress=progress)
        stage.REPORT_SECONDS = 0
        self.assertEqual(stage.run()["publications"], 2)
        self.assertTrue(observed)
        self.assertEqual(self.server.counts["/refused"], 1)
        self.assertTrue(all(row.processing["code_links"].status == ProcessingStatus.COMPLETED
                            for row in self.prepared.read_models("publications", Publication)))

    def test_an_alternative_pdf_source_releases_the_first_host(self):
        self._check_fallback_releases_the_previous_host(crawler=False)

    def test_the_crawler_fallback_releases_the_first_host(self):
        self._check_fallback_releases_the_previous_host(crawler=True)

    def test_converging_fallbacks_do_not_occupy_workers_waiting_for_the_same_host(self):
        other = self.url.replace("127.0.0.1", "localhost")
        self.prepared.write_models("publications", [
            Publication(id=identifier, title="fallback", pdf_urls=[self.url + "/refused", other + "/held"])
            for identifier in ("W1", "W2")
        ] + [Publication(id="W3", title="available host", pdf_urls=[self.url + "/pdf"])])
        observed = []

        def progress(_label, done, _total):
            if done and not observed:
                rows = {row.id: row for row in self.prepared.read_models("publications", Publication)}
                self.assertEqual(rows["W3"].processing["code_links"].status, ProcessingStatus.COMPLETED)
                self.assertNotIn("code_links", rows["W1"].processing)
                self.assertNotIn("code_links", rows["W2"].processing)
                observed.append(done)
                self.server.release.set()

        config = Settings(**(self.config.__dict__ | {"code_links_concurrency": 2, "pdf_host_concurrency": 1}))
        stage = CodeLinksStage(self.prepared, self.raw, config=config, on_progress=progress)
        stage.REPORT_SECONDS = 0
        self.assertEqual(stage.run()["publications"], 3)
        self.assertTrue(observed)

    def test_a_fallback_waiting_for_a_busy_host_leaves_a_worker_for_an_available_host(self):
        other = self.url.replace("127.0.0.1", "localhost")
        self.prepared.write_models("publications", [
            Publication(id="W1", title="fallback", pdf_urls=[self.url + "/refused", other + "/held"]),
            Publication(id="W2", title="busy fallback host", pdf_urls=[other + "/held"]),
            Publication(id="W3", title="first fast", pdf_urls=[self.url + "/pdf"]),
            Publication(id="W4", title="next fast", pdf_urls=[self.url + "/pdf"]),
        ])
        observed = []

        def progress(_label, done, _total):
            if done and not observed:
                rows = {row.id: row for row in self.prepared.read_models("publications", Publication)}
                if "code_links" in rows["W2"].processing:
                    self.assertIn("code_links", rows["W4"].processing)
                if "code_links" in rows["W4"].processing:
                    self.assertEqual(rows["W4"].processing["code_links"].status, ProcessingStatus.COMPLETED)
                    self.assertNotIn("code_links", rows["W2"].processing)
                    observed.append(done)
                    self.server.release.set()

        config = Settings(**(self.config.__dict__ | {"code_links_concurrency": 2, "pdf_host_concurrency": 1}))
        stage = CodeLinksStage(self.prepared, self.raw, config=config, on_progress=progress)
        stage.REPORT_SECONDS = 0
        self.assertEqual(stage.run()["publications"], 4)
        self.assertTrue(observed)
        self.assertTrue(all(row.processing["code_links"].status == ProcessingStatus.COMPLETED
                            for row in self.prepared.read_models("publications", Publication)))

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

    def test_a_slow_cache_write_does_not_block_another_publication(self):
        self.prepared.write_models("publications", [
            Publication(id="W1", title="slow cache", pdf_urls=[self.url + "/pdf"]),
            Publication(id="W2", title="fast cache", pdf_urls=[self.url + "/after-cache"]),
        ])
        config = Settings(**(self.config.__dict__ | {
            "code_links_concurrency": 2, "pdf_read_timeout": 3, "pdf_download_deadline": 5,
        }))
        store = PdfStore(self.db, config.pdf_dir)
        original_save = store.save
        observed = []

        def save(identifier, data):
            if identifier == "W1":
                self.server.held.set()
                if not self.server.release.wait(2):
                    raise RuntimeError("cache write blocked the coordinator")
            return original_save(identifier, data)

        def progress(_label, done, _total):
            if done and not observed:
                rows = {row.id: row for row in self.prepared.read_models("publications", Publication)}
                self.assertEqual(rows["W2"].processing["code_links"].status, ProcessingStatus.COMPLETED)
                self.assertNotIn("code_links", rows["W1"].processing)
                observed.append(done)
                self.server.release.set()

        stage = CodeLinksStage(self.prepared, self.raw, config=config, on_progress=progress)
        stage.REPORT_SECONDS = 0
        with patch("pauk.pipeline.stages.code_links.PdfStore", return_value=store), patch.object(store, "save", side_effect=save):
            self.assertEqual(stage.run()["publications"], 2)
        self.assertTrue(observed)

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

    def test_cache_write_retries_reuse_the_same_pdf_without_trying_another_url(self):
        for recover in (True, False):
            with self.subTest(recover=recover):
                self.server.counts.clear()
                identifier = "W-recover" if recover else "W-fail"
                self.prepared.write_models("publications", [Publication(
                    id=identifier, title="paper", abstract="Code: https://github.com/org/abstract",
                    pdf_urls=[self.url + "/pdf", self.url + "/chunked-gzip"],
                )])
                store = PdfStore(self.db, self.config.pdf_dir)
                original_save = store.save
                attempts = []

                def save(publication_id, data, *, attempts=attempts, recover=recover, original_save=original_save):
                    attempts.append((publication_id, data))
                    if not recover:
                        raise OSError("cache unavailable")
                    if len(attempts) == 1:
                        raise AutoReconnect("cache unavailable")
                    return original_save(publication_id, data)

                with patch("pauk.pipeline.stages.code_links.PdfStore", return_value=store), patch.object(
                    store, "save", side_effect=save,
                ):
                    CodeLinksStage(self.prepared, self.raw, config=self.config).run()
                self.assertEqual(attempts, [(identifier, self.server.pdf)] * 2)
                self.assertEqual(self.server.counts["/pdf"], 1)
                self.assertEqual(self.server.counts["/chunked-gzip"], 0)
                publication = next(self.prepared.read_models("publications", Publication))
                expected = ProcessingStatus.COMPLETED if recover else ProcessingStatus.FAILED
                self.assertEqual(publication.processing["code_links"].status, expected)
                links = next(self.prepared.read_models("repo_links", RepoLink))
                self.assertIn("https://github.com/org/abstract", [link.url for link in links.links])
                if recover:
                    self.assertEqual(store.read(identifier), self.server.pdf)
                else:
                    self.assertIn("cache unavailable", publication.processing["code_links"].error)
                    self.assertFalse(store.exists(identifier))

    def test_initial_sources_are_interleaved_when_the_host_cap_can_fill_the_pool(self):
        other = self.url.replace("127.0.0.1", "localhost")
        self.prepared.write_models("publications", [
            Publication(id="W1", title="slow", pdf_urls=[self.url + "/held"]),
            Publication(id="W2", title="same slow host", pdf_urls=[self.url + "/held"]),
            Publication(id="W3", title="other host", pdf_urls=[other + "/pdf"]),
        ])
        observed = []

        def progress(_label, done, _total):
            if done and not observed:
                rows = {row.id: row for row in self.prepared.read_models("publications", Publication)}
                self.assertIn("code_links", rows["W3"].processing)
                self.assertNotIn("code_links", rows["W1"].processing)
                self.assertNotIn("code_links", rows["W2"].processing)
                observed.append(done)
                self.server.release.set()

        config = Settings(**(self.config.__dict__ | {"code_links_concurrency": 2, "pdf_host_concurrency": 2}))
        stage = CodeLinksStage(self.prepared, self.raw, config=config, on_progress=progress)
        stage.REPORT_SECONDS = 0
        self.assertEqual(stage.run()["publications"], 3)
        self.assertTrue(observed)

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
        self.assertTrue(all(client._cancelled.is_set() and not client._active_reads for client in stage.http._clients))
        rows["W1"].pdf_urls = [self.url + "/pdf"]
        self.prepared.upsert_models("publications", [rows["W1"]])
        resumed = CodeLinksStage(self.prepared, self.raw, config=self.config).run()
        self.assertEqual(resumed["publications"], 1)
        saved = {row.id: row for row in self.prepared.read_models("publications", Publication)}
        self.assertEqual(set(saved), {"W1", "W2"})
        self.assertEqual(saved["W2"].processing["code_links"].attempts, 1)

    def test_a_retry_pause_leaves_the_worker_free_for_another_host(self):
        other = self.url.replace("127.0.0.1", "localhost")
        self.prepared.write_models("publications", [
            Publication(id="W1", title="rate limited", pdf_urls=[self.url + "/pause-once"]),
            Publication(id="W2", title="ready", pdf_urls=[other + "/pdf"]),
        ])
        config = Settings(**(self.config.__dict__ | {"code_links_concurrency": 1, "pdf_retries": 1}))
        first = []

        def progress(_label, done, _total):
            if done == 1 and not first:
                first.extend(row.id for row in self.prepared.read_models("publications", Publication)
                             if "code_links" in row.processing)
                self.assertEqual(self.server.counts["/pause-once"], 1)

        stage = CodeLinksStage(self.prepared, self.raw, config=config, on_progress=progress)
        stage.REPORT_SECONDS = 0
        self.assertEqual(stage.run()["publications"], 2)
        self.assertEqual(first, ["W2"])
        self.assertEqual(self.server.counts["/pause-once"], 2)

    def test_a_paused_redirect_target_does_not_block_the_worker_or_repeat_the_redirect(self):
        target = self.url.replace("127.0.0.1", "localhost") + "/redirect-target"
        self.client.rate_limiter._state("localhost", RateLimit())
        self.client.rate_limiter.pause("localhost", 0.5)
        self.prepared.write_models("publications", [
            Publication(id="W1", title="redirect", pdf_urls=[self.url + "/redirect?to=" + quote(target, safe="")]),
            Publication(id="W2", title="ready", pdf_urls=[self.url + "/pdf"]),
        ])
        config = Settings(**(self.config.__dict__ | {"code_links_concurrency": 1}))
        first = []

        def progress(_label, done, _total):
            if done == 1 and not first:
                first.extend(row.id for row in self.prepared.read_models("publications", Publication)
                             if "code_links" in row.processing)

        stage = CodeLinksStage(self.prepared, self.raw, config=config, on_progress=progress)
        stage.REPORT_SECONDS = 0
        self.assertEqual(stage.run()["publications"], 2)
        self.assertEqual(first, ["W2"])
        self.assertEqual(self.server.counts["/redirect"], 1)
        self.assertEqual(self.server.counts["/redirect-target"], 1)

    def test_cancellation_saves_an_already_cached_pdf_without_reparsing(self):
        self.prepared.write_models("publications", [Publication(id="W1", title="paper", pdf_urls=[self.url + "/pdf"])])
        store = PdfStore(self.db, self.config.pdf_dir)

        def progress(*_args):
            if store.exists("W1"):
                raise RuntimeError("stop requested")

        stage = CodeLinksStage(self.prepared, self.raw, config=self.config, on_progress=progress)
        stage.REPORT_SECONDS = 0
        with patch("pauk.pipeline.stages.code_links.PdfStore", return_value=store), self.assertRaisesRegex(RuntimeError, "stop requested"):
            stage.run()
        saved = next(self.prepared.read_models("publications", Publication))
        self.assertEqual(saved.processing["code_links"].status, ProcessingStatus.COMPLETED)
        self.assertEqual(CodeLinksStage(self.prepared, self.raw, config=self.config).run()["publications"], 0)

    def test_interrupted_forced_replacement_is_reprocessed_without_force(self):
        complete = ProcessingState(status=ProcessingStatus.COMPLETED, attempts=2)
        self.prepared.write_models("publications", [Publication(
            id="W1", title="paper", abstract="https://github.com/org/new", _processing={
                "code_links": complete, "link_relevance": complete,
            },
        )])
        self.prepared.write_models("repo_links", [RepoLink(
            publication_id="W1", links=[CodeLink(url="https://github.com/org/old", host="github.com")],
        )])
        upsert = self.prepared.upsert_models

        def interrupted(entity, rows):
            rows = list(rows)
            if entity == "publications" and rows[0].processing["code_links"].status == ProcessingStatus.COMPLETED:
                raise RuntimeError("save interrupted")
            upsert(entity, rows)

        with patch.object(self.prepared, "upsert_models", side_effect=interrupted), self.assertRaisesRegex(RuntimeError, "save interrupted"):
            CodeLinksStage(self.prepared, self.raw, config=self.config, force=True).run()
        self.assertEqual(next(self.prepared.read_models("repo_links", RepoLink)).links[0].url, "https://github.com/org/new")
        saved = next(self.prepared.read_models("publications", Publication))
        self.assertNotIn("link_relevance", saved.processing)
        self.assertEqual(CodeLinksStage(self.prepared, self.raw, config=self.config).run()["publications"], 1)
        self.assertEqual(next(self.prepared.read_models("publications", Publication)).processing["code_links"].attempts, 3)

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
        for identifier in ("W3", "W4"):
            (self.config.pdf_dir / f"{identifier}.pdf").write_bytes(self.server.pdf)
        self.prepared.write_models("publications", [
            Publication(id="W1", title="first", merged_ids=["W3"]),
            Publication(id="W2", title="second", merged_ids=["W4"]),
        ])
        CodeLinksStage(self.prepared, self.raw, config=self.config).run()
        for publication in self.prepared.read_models("publications", Publication):
            self.assertEqual(publication.processing["code_links"].status, ProcessingStatus.COMPLETED)
        links = {row.publication_id: row for row in self.prepared.read_models("repo_links", RepoLink)}
        for pub_id, pdf_id in (("W1", "W3"), ("W2", "W4")):
            self.assertEqual({occ.pdf_id for link in links[pub_id].links for occ in link.occurrences}, {pdf_id})
        self.assertFalse(self.server.counts)


class HttpsPdfTest(LocalPdfTest):
    secure = True

    def test_deadline_interrupts_https_headers_and_bodies(self):
        for path in ("/headers-drip", "/drip", "/drip?close=1"):
            with self.subTest(path=path):
                started = time.monotonic()
                with self.assertRaisesRegex(HttpRequestError, "download deadline exceeded"):
                    self.fetch(path, timeout=1, deadline=0.15)
                self.assertLess(time.monotonic() - started, 0.65)
                self.assertFalse(self.client._active_reads)
                self.assertEqual(self.fetch("/pdf"), self.server.pdf)

    def test_cancellation_interrupts_header_reads_before_the_socket_timeout(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.fetch, "/headers-drip", timeout=2, deadline=2)
            until = time.monotonic() + 1
            while not self.server.counts["/headers-drip"] and time.monotonic() < until:
                time.sleep(0.005)
            self.assertTrue(self.server.counts["/headers-drip"])
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
