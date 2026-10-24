"""HTTP front of the read-only live dashboard: standard library only, 127.0.0.1 only, GET only."""
import ipaddress
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from oncall import config
from oncall.ui import data

PAGE = Path(__file__).with_name("index.html")


def route(path):
    """(status, content_type, body) for a GET path. Pure function, so it is easy to test."""
    path = unquote(urlsplit(path).path)
    if path in ("/", "/index.html"):
        return 200, "text/html; charset=utf-8", PAGE.read_bytes()
    if path == "/api/health":
        return _json(data.health())
    if path == "/api/status":
        return _json(data.status())
    if path == "/api/incidents":
        return _json(data.incidents())
    if path == "/api/actions":
        return _json(data.actions())
    if path == "/api/notifications":
        return _json(data.notifications())
    if path in ("/api/incident", "/api/incident/"):
        record = data.incident()
        return _json(record) if record else _json({"error": "no runs yet"}, 404)
    if path.startswith("/api/incident/"):
        record = data.incident(path.rsplit("/", 1)[-1])
        return _json(record) if record else _json({"error": "no such run"}, 404)
    if path.startswith("/api/report/"):
        text = data.report_text(path.rsplit("/", 1)[-1])
        if text is None:
            return 404, "text/plain; charset=utf-8", b"no such run"
        return 200, "text/plain; charset=utf-8", text.encode("utf-8")
    return _json({"error": "not found"}, 404)


def _json(obj, status=200):
    return status, "application/json; charset=utf-8", json.dumps(obj, default=str).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "oncall-ui/1"

    def do_GET(self):
        try:
            status, ctype, body = route(self.path)
        except Exception as e:  # never show a traceback; the page keeps polling
            status, ctype, body = _json({"error": f"internal error: {type(e).__name__}"}, 500)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline' 'self'; "
                                                    "style-src 'unsafe-inline' 'self'; connect-src 'self'")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # read-only: nothing can be changed through the dashboard
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_PUT = do_DELETE = do_PATCH = do_POST

    def log_message(self, fmt, *args):
        pass


def make_server(host="127.0.0.1", port=None):
    if not ipaddress.ip_address(host).is_loopback:
        raise ValueError(f"refusing to bind {host}: the dashboard listens on localhost only")
    return ThreadingHTTPServer((host, config.UI_PORT if port is None else port), Handler)
