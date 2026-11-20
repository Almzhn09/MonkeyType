"""Loopback-only static server: dashboard assets plus the sanitized bundle directory.

Nothing else on disk is reachable. Data paths must match the bundle layout exactly, be JSON
and resolve inside the output directory.
"""

from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

STATIC = Path(__file__).with_name("static")
ASSETS = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js",
          "/lib.js": "lib.js", "/styles.css": "styles.css"}
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8"}
SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
DATA = re.compile(
    rf"^/data/(index\.json|bundles/{SEGMENT}/bundle\.json"
    rf"|bundles/{SEGMENT}/episodes/{SEGMENT}/{SEGMENT}\.json"
    rf"|bundles/{SEGMENT}/activity/{SEGMENT}/{SEGMENT}/{SEGMENT}\.json)$"
)


def make_handler(out_dir):
    out_dir = Path(out_dir).resolve()

    class Handler(SimpleHTTPRequestHandler):
        server_version = "MonkeyTypeDashboard"

        def log_message(self, *_):
            pass

        def _file(self, path):
            body = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", TYPES.get(path.suffix, "application/octet-stream"))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                             "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _deny(self):
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")

        def do_GET(self):
            path = unquote(urlsplit(self.path).path)
            if path in ASSETS:
                return self._file(STATIC / ASSETS[path])
            if ".." in path or not DATA.match(path):
                return self._deny()
            target = (out_dir / path[len("/data/"):]).resolve()
            if not target.is_relative_to(out_dir) or not target.is_file():
                return self._deny()
            return self._file(target)

        do_HEAD = do_GET

        def do_POST(self):
            self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)

        do_PUT = do_DELETE = do_PATCH = do_POST

        def list_directory(self, path):  # never browse directories
            self._deny()

    return Handler


def make_server(out_dir, port=8770):
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(out_dir))
