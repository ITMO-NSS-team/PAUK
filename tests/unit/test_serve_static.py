"""scripts/serve_static.py: precompressed static serving for the deployed site.

Loaded from scripts/ by path: the directory is a collection of operator
tools, not an importable package.
"""

import gzip
import http.client
import importlib.util
import os
import sys
import tempfile
import threading
import unittest
from functools import partial
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "serve_static.py"
_spec = importlib.util.spec_from_file_location("serve_static", SCRIPT)
assert _spec is not None and _spec.loader is not None
serve_static = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = serve_static
_spec.loader.exec_module(serve_static)

DATA = b'{"authors": [' + b'{"key": "A1"},' * 2000 + b"{}]}"


class _QuietHandler(serve_static.PrecompressedHandler):
    def log_message(self, *args):
        pass


class ServeStaticTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "graph-data.json").write_bytes(DATA)
        (root / "graph-data.json.gz").write_bytes(gzip.compress(DATA))
        (root / "index.html").write_bytes(b"<html></html>")  # no .gz next to it
        handler = partial(_QuietHandler, directory=str(root))
        self.server = serve_static.ThreadingServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def get(self, path, **headers):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1])
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response, body

    def test_browser_accepting_gzip_gets_the_precompressed_file(self):
        response, body = self.get("/graph-data.json", **{"Accept-Encoding": "gzip, deflate, br"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Encoding"), "gzip")
        self.assertEqual(response.getheader("Content-Type"), "application/json")
        self.assertEqual(response.getheader("Content-Length"), str(len(body)))
        self.assertLess(len(body), len(DATA))
        self.assertEqual(gzip.decompress(body), DATA)

    def test_client_without_gzip_gets_the_plain_file(self):
        response, body = self.get("/graph-data.json")
        self.assertIsNone(response.getheader("Content-Encoding"))
        self.assertEqual(body, DATA)

    def test_file_without_gz_is_served_as_usual(self):
        response, body = self.get("/index.html", **{"Accept-Encoding": "gzip"})
        self.assertEqual(response.status, 200)
        self.assertIsNone(response.getheader("Content-Encoding"))
        self.assertEqual(body, b"<html></html>")

    def test_unchanged_file_answers_304_so_repeat_visits_download_nothing(self):
        first, _ = self.get("/graph-data.json", **{"Accept-Encoding": "gzip"})
        again, body = self.get(
            "/graph-data.json", **{"Accept-Encoding": "gzip", "If-Modified-Since": first.getheader("Last-Modified")}
        )
        self.assertEqual(again.status, 304)
        self.assertEqual(body, b"")

    def test_changed_file_is_sent_again(self):
        first, _ = self.get("/graph-data.json", **{"Accept-Encoding": "gzip"})
        gz = Path(self.tmp.name) / "graph-data.json.gz"
        later = os.stat(gz).st_mtime + 60
        os.utime(gz, (later, later))
        again, body = self.get(
            "/graph-data.json", **{"Accept-Encoding": "gzip", "If-Modified-Since": first.getheader("Last-Modified")}
        )
        self.assertEqual(again.status, 200)
        self.assertEqual(gzip.decompress(body), DATA)

    def test_missing_file_is_still_404(self):
        response, _ = self.get("/nope.json", **{"Accept-Encoding": "gzip"})
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
