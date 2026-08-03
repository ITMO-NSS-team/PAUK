"""python3 visualization/serve.py [port] - static file server with gzip compression.

Serves web/ as-is. The one dynamic route is /api/stats: it recomputes the DB
statistics straight from Neo4j (generate_stats.snapshot), refreshes
web/graph-stats.js so a plain page reload shows the same numbers, and returns
them as JSON — that is what the "Пересчитать" button on the health tab calls.
Everything else on the page stays a static file with no backend behind it.
"""

import gzip
import io
import json
import mimetypes
import sys
import traceback
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8501
ROOT = Path(__file__).parent / "web"
API_STATS = "/api/stats"


class GzipHandler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def _send_json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _recompute_stats(self):
        """Fresh snapshot from Neo4j. Imported lazily so that a machine without
        the neo4j driver installed can still serve the static site."""
        try:
            import generate_stats
        except ImportError as e:
            return self._send_json(503, {
                "error": "На сервере не установлен драйвер Neo4j — "
                         "пересчёт недоступен, показаны сохранённые числа.",
                "detail": str(e)})
        try:
            stats = generate_stats.snapshot()
            generate_stats.write_js(stats, ROOT)
        except Exception as e:
            print("stats recompute failed:", traceback.format_exc(), file=sys.stderr)
            return self._send_json(503, {
                "error": "Не удалось получить данные из Neo4j. "
                         "Проверьте, что база запущена.",
                "detail": f"{type(e).__name__}: {e}"})
        self._send_json(200, stats)

    def do_POST(self):
        if self.path.split("?")[0] == API_STATS:
            return self._recompute_stats()
        self.send_error(404)

    def do_GET(self):
        if self.path.split("?")[0] == API_STATS:
            return self._recompute_stats()

        path = self.translate_path(self.path)
        if not Path(path).is_file():
            return super().do_GET()

        accepts_gzip = "gzip" in self.headers.get("Accept-Encoding", "")
        data = Path(path).read_bytes()

        if accepts_gzip:
            buf = io.BytesIO()
            with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6) as gz:
                gz.write(data)
            compressed = buf.getvalue()
        else:
            compressed = None

        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        body = compressed if compressed and len(compressed) < len(data) else data

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if body is compressed:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client navigated away / dropped connection mid-download — not our problem

    def log_message(self, fmt, *args):
        pass  # quiet mode


if __name__ == "__main__":
    # threaded: a slow/large download (graph-data.js is 5MB+) from one client
    # must not block every other request behind it
    server = ThreadingHTTPServer(("", PORT), GzipHandler)
    print(f"http://0.0.0.0:{PORT}  (gzip on)")
    server.serve_forever()
