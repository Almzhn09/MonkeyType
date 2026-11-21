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


def respond(handler, out_dir, path, frame_ancestors="'none'"):
    """Write the response for one GET/HEAD ``path`` (already stripped of any mount prefix)."""
    def send_file(file):
        body = file.read_bytes()
        handler.send_response(HTTPStatus.OK)
        handler.send_header("Content-Type", TYPES.get(file.suffix, "application/octet-stream"))
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Content-Security-Policy",
                            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                            f"connect-src 'self'; frame-ancestors {frame_ancestors}; base-uri 'none'; form-action 'none'")
        handler.send_header("Referrer-Policy", "no-referrer")
        handler.end_headers()
        if handler.command != "HEAD":
            handler.wfile.write(body)

    if path in ASSETS:
        return send_file(STATIC / ASSETS[path])
    if ".." in path or not DATA.match(path):
        return handler.send_error(HTTPStatus.NOT_FOUND, "Not found")
    target = (Path(out_dir).resolve() / path[len("/data/"):]).resolve()
    if not target.is_relative_to(Path(out_dir).resolve()) or not target.is_file():
        return handler.send_error(HTTPStatus.NOT_FOUND, "Not found")
    return send_file(target)


def make_handler(out_dir):
    out_dir = Path(out_dir).resolve()

    class Handler(SimpleHTTPRequestHandler):
        server_version = "MonkeyTypeDashboard"

        def log_message(self, *_):
            pass

        def do_GET(self):
            respond(self, out_dir, unquote(urlsplit(self.path).path))

        do_HEAD = do_GET

        def do_POST(self):
            self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)

        do_PUT = do_DELETE = do_PATCH = do_POST

        def list_directory(self, path):  # never browse directories
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    return Handler


def make_server(out_dir, port=8770):
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(out_dir))
