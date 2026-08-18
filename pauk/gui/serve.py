"""python -m pauk.gui.serve [port] [--public] - static file server with gzip compression.

Serves web/ as-is, with one exception: graph-data.js and graph-search.js carry
the PII generate_data.py's --public flag redacts (see that module), so they
never live in web/ - they're read from pauk/gui/data/private/ by default, or
from pauk/gui/data/public/ when this server is started with --public, mirroring
generate_data.py's own flag. graph-stats.js has no such split (aggregate counts
only) and keeps coming straight from web/, written there by generate_stats.py.

The one dynamic route is /api/stats: it recomputes the DB statistics straight
from Neo4j (generate_stats.snapshot), refreshes web/graph-stats.js so a plain
page reload shows the same numbers, and returns them as JSON - that is what
the "Пересчитать" button on the health tab calls. Everything else on the page
stays a static file with no backend behind it.
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
    "graph-data.js": (DATA_DIR, "python -m pauk.gui.generate_data" + (" --public" if PUBLIC else "")),
    "graph-search.js": (DATA_DIR, "python -m pauk.gui.generate_data" + (" --public" if PUBLIC else "")),
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
        # --public simulates the redacted, backend-less GitHub Pages build:
        # the health tab and its data never ship there (see generate_stats.py
        # not having a --public mode at all), so pretend none of it exists,
        # regardless of whether web/graph-stats.js happens to be sitting on
        # disk from an earlier internal run.
        if PUBLIC and (route.lstrip("/") == "graph-stats.js" or route in (API_STATS, API_CHECK)):
            return self.send_error(404)
        # Deliberately POST-only: recomputing queries Neo4j for several
        # seconds and rewrites web/graph-stats.js. A GET must stay safe to
        # repeat — browser prefetch, crawlers and proxies all issue them
        # unprompted. The page's "Пересчитать" button already POSTs.
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

        # "no-cache" alone tells the browser to revalidate, but with nothing to
        # revalidate against it may just reuse its copy — which is how an edited
        # index.html keeps serving the previous version. Give it a validator.
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
    # threaded: a slow/large download (graph-data.js is 5MB+) from one client
    # must not block every other request behind it
    server = ThreadingHTTPServer(("", PORT), GzipHandler)
    # Binds to all interfaces ("" above) — print the machine's actual network
    # name, not localhost, since this commonly runs on a remote lab server
    # and whoever started it needs the address other people can reach.
    print(f"http://{socket.gethostname()}:{PORT}  (gzip on)")
    server.serve_forever()
