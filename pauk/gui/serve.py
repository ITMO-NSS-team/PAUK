"""python -m pauk.gui.serve [port] [--public] - static file server"""

import gzip
import io
import json
import logging
import mimetypes
import socket
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from pauk.settings import settings

logger = logging.getLogger(__name__)

_argv = sys.argv[1:]
PUBLIC = "--public" in _argv
_port_args = [a for a in _argv if a != "--public"]
PORT = int(_port_args[0]) if _port_args else 8501
ROOT = Path(__file__).parent / "web"
# The same setting the rebuild writes to. Two copies of this path would let
# the worker write one directory and the server read another, and the map
# would quietly stay as it was.
DATA_DIR = settings.map_out_dir(PUBLIC)
API_STATS = "/api/stats"
API_CHECK = "/api/check"
API_ASK = "/api/ask"
ASK_MAX_BODY = 64 * 1024

# The search engine holds the embedding model and the indexes in memory, so it
# is built once per process - in the background at startup, or on the first
# question if the index appeared later.
_ask_engine = None
_ask_engine_error: str | None = None
_ask_engine_lock = threading.Lock()


def _get_ask_engine():
    global _ask_engine, _ask_engine_error
    with _ask_engine_lock:
        if _ask_engine is None:
            from pauk.search.pipeline import Engine

            try:
                _ask_engine = Engine(settings)
                _ask_engine_error = None
            except Exception as exc:
                _ask_engine_error = f"{type(exc).__name__}: {exc}"
                raise
        return _ask_engine


def _warm_ask_engine():
    if not (settings.search_dir / "manifest.json").is_file():
        logger.info("No search index in %s - the Ask tab needs `pauk search build-index`", settings.search_dir)
        return
    try:
        _get_ask_engine().embedder(["warm up"])
        logger.info("Search engine ready")
    except Exception:
        logger.exception("Search engine failed to start")

_gzip_cache: dict[str, tuple[str, bytes]] = {}

REQUIRED_FILES = {
    "graph-data.js": (
        DATA_DIR,
        "python -m pauk.gui.generate_data" + (" --public" if PUBLIC else ""),
    ),
    "graph-search.js": (
        DATA_DIR,
        "python -m pauk.gui.generate_data" + (" --public" if PUBLIC else ""),
    ),
    "graph-stats.js": (DATA_DIR, f"python -m pauk.gui.generate_stats --out-dir {DATA_DIR}"),
}

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
            logger.debug("Client dropped connection writing %s: %s", self.path, exc)

    def _recompute_stats(self):
        """Fresh snapshot from Neo4j. Imported lazily so that a machine without
        the neo4j driver installed can still serve the static site."""
        try:
            from . import generate_stats
        except ImportError as e:
            return self._send_json(
                503,
                {
                    "error": "The Neo4j driver is not installed on the server - recompute is not available.",
                    "detail": str(e),
                },
            )
        try:
            stats = generate_stats.snapshot()
            generate_stats.write_js(stats, DATA_DIR)
        except Exception as e:
            logger.exception("stats recompute failed")
            return self._send_json(
                503,
                {
                    "error": "Failed to retrieve data from Neo4j. Please make sure the database is running.",
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
                    "error": "The Neo4j driver is not installed on the server - samples are not available.",
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
            return self._send_json(404, {"error": f"Unknown check: {check_id}"})
        except ValueError as e:
            return self._send_json(404, {"error": str(e)})
        except Exception as e:
            logger.exception("Check examples failed")
            return self._send_json(
                503,
                {
                    "error": "Failed to retrieve data from Neo4j. Please make sure the database is running.",
                    "detail": f"{type(e).__name__}: {e}",
                },
            )

    def _ask(self):
        """One free-text question through pauk.search. Read-only on the graph."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if not 0 < length <= ASK_MAX_BODY:
            return self._send_json(400, {"error": "Request body is empty or too large"})
        try:
            from pauk.search.pipeline import AskOptions
            from pauk.search.plan import QueryPlan
            from pauk.search.retrieval import MODES
        except ImportError as e:
            return self._send_json(503, {"error": "Search dependencies are not installed.", "detail": str(e)})
        try:
            body = json.loads(self.rfile.read(length))
            question = str(body.get("question") or "").strip()[:2000]
            mode = body.get("retrieval_mode", "hybrid")
            ranking = body.get("ranking", "graph")
            if not question or mode not in MODES or ranking not in ("graph", "authorship"):
                raise ValueError("question, retrieval_mode or ranking is invalid")
            options = AskOptions(
                retrieval_mode=mode, ranking=ranking,
                llm_parse=bool(body.get("llm_parse", True)), llm_answer=bool(body.get("llm_answer", True)),
                top_n=max(1, min(100, int(body.get("top_n", 10)))),
                plan=QueryPlan.model_validate(body["plan"]) if body.get("plan") else None,
            )
        except (ValueError, TypeError, AttributeError) as e:
            return self._send_json(400, {"error": f"Bad request: {e}"})
        try:
            engine = _get_ask_engine()
        except Exception:
            return self._send_json(503, {
                "error": "The search index is not available. Build it with `pauk search build-index`.",
                "detail": _ask_engine_error,
            })
        try:
            return self._send_json(200, engine.ask(question, options))
        except Exception as e:
            logger.exception("ask failed")
            return self._send_json(503, {
                "error": "Search failed. Please make sure Neo4j is running.",
                "detail": f"{type(e).__name__}: {e}",
            })

    def do_POST(self):
        if PUBLIC:
            return self.send_error(404)
        route = self.path.split("?")[0]
        if route == API_STATS:
            return self._recompute_stats()
        if route == API_ASK:
            return self._ask()
        self.send_error(404)

    def do_GET(self):
        route = self.path.split("?")[0]
        # graph-stats.js is an aggregate-only snapshot (counts/percentages, no
        # individual rows) - safe to serve on --public. /api/check,
        # /api/stats and /api/ask hit a live Neo4j and can return raw rows
        # (names), so those stay blocked regardless of build.
        if PUBLIC and route in (API_STATS, API_CHECK, API_ASK):
            return self.send_error(404)
        if route == API_STATS:
            return self._send_json(405, {"error": "Recompute is POST-only"})
        if route == API_CHECK:
            return self._check_examples()

        if route.lstrip("/") in _DATA_DIR_FILES:
            path = str(DATA_DIR / route.lstrip("/"))
        else:
            path = self.translate_path(self.path)
        if not Path(path).is_file():
            return super().do_GET()

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
            logger.debug("Сlient dropped connection writing %s: %s", path, exc)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    _warn_missing_generated_files()
    if not PUBLIC:
        threading.Thread(target=_warm_ask_engine, name="ask-warmup", daemon=True).start()
    server = ThreadingHTTPServer(("", PORT), GzipHandler)
    mode = "PUBLIC (redacted)" if PUBLIC else "private (full data)"
    print(f"http://{socket.gethostname()}:{PORT}  ({mode}, data from {DATA_DIR})")
    server.serve_forever()
