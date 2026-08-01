"""python -m pauk.gui.serve [port] - static file server with gzip compression."""

import gzip
import io
import logging
import mimetypes
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

logger = logging.getLogger(__name__)

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8501
ROOT = Path(__file__).parent / "web"


class GzipHandler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def do_GET(self):
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
        except (BrokenPipeError, ConnectionResetError) as exc:
            logger.debug("client dropped connection writing %s: %s", path, exc)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # threaded: a slow/large download (graph-data.js is 5MB+) from one client
    # must not block every other request behind it
    server = ThreadingHTTPServer(("", PORT), GzipHandler)
    print(f"http://0.0.0.0:{PORT}  (gzip on)")
    server.serve_forever()
