"""Serve the built site from the current directory, precompressed where possible.

`python3 -m http.server` with one addition: when the browser accepts gzip and
`<file>.gz` sits next to `<file>`, the .gz is sent with `Content-Encoding:
gzip`. `deploy.sh` makes those .gz files after the build - the site's JSON
shrinks about five times over the wire.

Runs on the lab server's own python3, whose version isn't pinned, so this
file stays stdlib-only and avoids syntax newer than 3.6 (no `from __future__
import annotations`, unions only inside string annotations).

Usage: `cd <site dir> && python3 serve_static.py <port>`.
"""

import email.utils
import os
import socketserver
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from typing import BinaryIO


class PrecompressedHandler(SimpleHTTPRequestHandler):
    def send_head(self) -> "BinaryIO | None":
        path = self.translate_path(self.path)
        gz_path = path + ".gz"
        accepts_gzip = "gzip" in self.headers.get("Accept-Encoding", "")
        if not (accepts_gzip and os.path.isfile(path) and os.path.isfile(gz_path)):
            return super().send_head()

        mtime = int(os.stat(gz_path).st_mtime)
        if self._not_modified_since(mtime):
            self.send_response(304)
            self.send_header("Vary", "Accept-Encoding")
            self.end_headers()
            return None

        # Returned open, as in the stdlib: the base handler copies it out and closes it.
        f = open(gz_path, "rb")  # noqa: SIM115
        self.send_response(200)
        # The type of the original file, not of the .gz.
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(os.fstat(f.fileno()).st_size))
        self.send_header("Last-Modified", self.date_time_string(mtime))
        self.send_header("Vary", "Accept-Encoding")
        self.end_headers()
        return f

    def _not_modified_since(self, mtime: int) -> bool:
        header = self.headers.get("If-Modified-Since")
        if not header:
            return False
        try:
            since = email.utils.parsedate_to_datetime(header)
        except (TypeError, ValueError, IndexError):
            return False
        return since is not None and since.timestamp() >= mtime


class ThreadingServer(socketserver.ThreadingMixIn, HTTPServer):
    # A 46 MB download must not block every other visitor.
    daemon_threads = True


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    server = ThreadingServer(("", port), PrecompressedHandler)
    print(f"Serving {os.getcwd()} on port {port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
