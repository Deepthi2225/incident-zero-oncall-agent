"""HTTP front of the Action API: standard library only (http.server + json), localhost only."""
import ipaddress
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from actionapi import config
from actionapi.service import ActionService

MAX_BODY = 64 * 1024


def make_handler(service):
    class Handler(BaseHTTPRequestHandler):
        server_version = "actionapi/1"

        def _reply(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _handle(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._reply(413, {"ok": False, "error": "body too large"})
            body = self.rfile.read(length) if length else b""
            status, response = service.handle(self.command, self.path, dict(self.headers), body,
                                              caller=f"{self.client_address[0]}:{self.client_address[1]}")
            self._reply(status, response)

        do_GET = do_POST = do_PUT = do_DELETE = _handle

        def log_message(self, fmt, *args):  # the audit log replaces the default stderr log
            pass

    return Handler


def make_server(service=None, host=None, port=None):
    """A ready server (not started). Refuses any address that is not loopback."""
    host = host or config.HOST
    if not ipaddress.ip_address(host).is_loopback:
        raise ValueError(f"refusing to bind {host}: the Action API listens on localhost only")
    service = service or ActionService()
    return ThreadingHTTPServer((host, config.PORT if port is None else port), make_handler(service))
