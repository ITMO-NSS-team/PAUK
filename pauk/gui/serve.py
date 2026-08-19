"""python -m pauk.gui.serve [port] [--public] - static file server with gzip compression.

graph-data.js/graph-search.js are read from pauk/gui/data/private/ (default)
or pauk/gui/data/public/ (--public), mirroring generate_data.py's own flag.
graph-stats.js has no such split and comes straight from web/.

/api/stats recomputes DB stats from Neo4j and rewrites web/graph-stats.js;
everything else is a static file with no backend.
"""

import gzip
import io
import json
import logging
import mimetypes
import socket
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

_argv = sys.argv[1:]
PUBLIC = "--public" in _argv
_port_args = [a for a in _argv if a != "--public"]
PORT = int(_port_args[0]) if _port_args else 8501
ROOT = Path(__file__).parent / "web"
DATA_DIR = Path(__file__).parent / "data" / ("public" if PUBLIC else "private")
API_STATS = "/api/stats"
API_CHECK = "/api/check"

_gzip_cache: dict[str, tuple[str, bytes]] = {}

# name -> (directory it's served from, command to generate it)
REQUIRED_FILES = {
    "graph-data.js": (
        DATA_DIR,
        "python -m pauk.gui.generate_data" + (" --public" if PUBLIC else ""),
    ),
    "graph-search.js": (
        DATA_DIR,
        "python -m pauk.gui.generate_data" + (" --public" if PUBLIC else ""),
    ),
    "graph-stats.js": (ROOT, "python -m pauk.gui.generate_stats"),
}
# Requests for these filenames are rerouted from ROOT to DATA_DIR in do_GET.
_DATA_DIR_FILES = {name for name, (d, _cmd) in REQUIRED_FILES.items() if d == DATA_DIR}


def _warn_missing_generated_files():
    missing = {name: cmd for name, (d, cmd) in REQUIRED_FILES.items() if not (d / name).is_file()}
    if not missing:
        return
    logger.warning("Generated files not found, the page will be incomplete:")
    for cmd in dict.fromkeys(missing.values()):
        logger.warning("  %s", cmd)


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
        except (BrokenPipeError, ConnectionResetError) as exc:
            logger.debug("client dropped connection writing %s: %s", self.path, exc)

    def _recompute_stats(self):
        """Fresh snapshot from Neo4j. Imported lazily so that a machine without
        the neo4j driver installed can still serve the static site."""
        try:
            from . import generate_stats
        except ImportError as e:
            return self._send_json(
                503,
                {
                    "error": "На сервере не установлен драйвер Neo4j — "
                    "пересчёт недоступен, показаны сохранённые числа.",
                    "detail": str(e),
                },
            )
        try:
            stats = generate_stats.snapshot()
            generate_stats.write_js(stats, ROOT)
        except Exception as e:
            logger.exception("stats recompute failed")
            return self._send_json(
                503,
                {
                    "error": "Не удалось получить данные из Neo4j. Проверьте, что база запущена.",
                    "detail": f"{type(e).__name__}: {e}",
                },
            )
        self._send_json(200, stats)

    def _check_examples(self):
        """Rows behind one check — feeds the details popup and its CSV export."""
        try:
            from . import generate_stats
        except ImportError as e:
            return self._send_json(
                503,
                {
                    "error": "На сервере не установлен драйвер Neo4j — примеры недоступны.",
                    "detail": str(e),
                },
            )

        qs = parse_qs(urlparse(self.path).query)
        check_id = (qs.get("id") or [""])[0]
        try:
            limit = int((qs.get("limit") or ["0"])[0]) or generate_stats.EXAMPLES_LIMIT_DEFAULT
        except ValueError:
            limit = generate_stats.EXAMPLES_LIMIT_DEFAULT

        try:
            return self._send_json(200, generate_stats.examples(check_id, limit))
        except KeyError:
            return self._send_json(404, {"error": f"Неизвестная проверка: {check_id}"})
        except ValueError as e:
            return self._send_json(404, {"error": str(e)})
        except Exception as e:
            logger.exception("check examples failed")
            return self._send_json(
                503,
                {
                    "error": "Не удалось получить данные из Neo4j. Проверьте, что база запущена.",
                    "detail": f"{type(e).__name__}: {e}",
                },
            )

    def do_POST(self):
        if PUBLIC:
            return self.send_error(404)
        if self.path.split("?")[0] == API_STATS:
            return self._recompute_stats()
        self.send_error(404)

    def do_GET(self):
        route = self.path.split("?")[0]
        # --public simulates the backend-less GitHub Pages build: the health
        # tab never ships there, even if web/graph-stats.js is on disk locally.
        if PUBLIC and (route.lstrip("/") == "graph-stats.js" or route in (API_STATS, API_CHECK)):
            return self.send_error(404)
        # GET must stay safe to repeat (prefetch, crawlers) — recompute is POST-only.
        if route == API_STATS:
            return self._send_json(405, {"error": "Пересчёт доступен только методом POST."})
        if route == API_CHECK:
            return self._check_examples()

        if route.lstrip("/") in _DATA_DIR_FILES:
            path = str(DATA_DIR / route.lstrip("/"))
        else:
            path = self.translate_path(self.path)
        if not Path(path).is_file():
            return super().do_GET()

        # ETag gives the browser something to revalidate "no-cache" against.
        st = Path(path).stat()
        etag = f'"{int(st.st_mtime)}-{st.st_size}"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return

        accepts_gzip = "gzip" in self.headers.get("Accept-Encoding", "")
        gz_bytes = None
        if accepts_gzip:
            cached = _gzip_cache.get(path)
            if cached and cached[0] == etag:
                gz_bytes = cached[1]
            else:
                buf = io.BytesIO()
                with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6) as gz:
                    gz.write(Path(path).read_bytes())
                gz_bytes = buf.getvalue()
                _gzip_cache[path] = (etag, gz_bytes)

        # gzip loses on already-compressed/tiny content; fall back to the raw file
        if gz_bytes is not None and len(gz_bytes) < st.st_size:
            body: bytes = gz_bytes
            use_gzip = True
        else:
            body = Path(path).read_bytes()
            use_gzip = False

        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("ETag", etag)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError) as exc:
            logger.debug("client dropped connection writing %s: %s", path, exc)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    _warn_missing_generated_files()
    server = ThreadingHTTPServer(
        ("", PORT), GzipHandler
    )  # threaded: large downloads shouldn't block others
    mode = "PUBLIC (redacted)" if PUBLIC else "private (full data)"
    print(f"http://{socket.gethostname()}:{PORT}  (gzip on, {mode}, data from {DATA_DIR})")
    server.serve_forever()
