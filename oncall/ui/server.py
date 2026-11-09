"""HTTP front of the dashboard: standard library only, 127.0.0.1 only, every page behind a login.

Protections: scrypt-hashed accounts and login throttling (auth.py), HttpOnly SameSite=Strict session
cookies, a CSRF token on every change, Host and Origin checks against DNS rebinding and other sites,
small JSON-only request bodies, role checks on every action, and an audit log of every attempt.
"""
import hmac
import ipaddress
import json
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from oncall import config
from oncall.record import append_line, now_iso
from oncall.ui import actions, auth, data

PAGE = Path(__file__).with_name("index.html")
LOGIN = Path(__file__).with_name("login.html")
COOKIE = "poppy_session"
MAX_BODY = 8192
SECRET_FIELDS = ("password",)
sessions, throttle = auth.Sessions(), auth.Throttle()


def route(path, user=None):
    """(status, content_type, body) for a signed-in GET path. Pure function, so it is easy to test."""
    path = unquote(urlsplit(path).path)
    if path in ("/", "/index.html"):
        return 200, "text/html; charset=utf-8", PAGE.read_bytes()
    if path == "/api/health":
        return _json(data.health())
    if path == "/api/status":
        return _json(data.status())
    if path == "/api/incidents":
        return _json(data.incidents(limit=200))
    if path == "/api/actions":
        return _json(data.actions())
    if path == "/api/notifications":
        return _json(data.notifications())
    if path == "/api/settings":
        return _json(data.settings())
    if path == "/api/audit":
        if user is not None and not auth.can(user["role"], "approver"):
            return _json({"error": "not allowed"}, 403)
        return _json(data.audit())
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


def audit(user, action, status, detail=None):
    clean = {k: v for k, v in (detail or {}).items() if k not in SECRET_FIELDS}
    append_line(config.RUNS_DIR / "ui_audit.jsonl", {"ts": now_iso(), "user": user, "action": action,
                                                     "status": status, "detail": clean})


class Handler(BaseHTTPRequestHandler):
    server_version = "poppy-ui/2"
    sys_version = ""
    extra_headers = ()

    def _send(self, status, ctype, body):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline' 'self'; "
                                                    "style-src 'unsafe-inline' 'self'; connect-src 'self'; "
                                                    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for name, value in self.extra_headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _hosts(self):
        port = self.server.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _host_ok(self):
        return self.headers.get("Host", "") in self._hosts()

    def _token(self):
        try:
            morsel = SimpleCookie(self.headers.get("Cookie", "")).get(COOKIE)
        except CookieError:
            return None
        return morsel.value if morsel else None

    def _session(self):
        return sessions.get(self._token())

    def do_GET(self):
        self.extra_headers = ()
        if not self._host_ok():
            return self._send(*_json({"error": "bad host"}, 403))
        try:
            path = unquote(urlsplit(self.path).path)
            if path == "/login":
                return self._send(200, "text/html; charset=utf-8", LOGIN.read_bytes())
            if path == "/api/setup":
                return self._send(*_json({"needs_account": not auth.users()}))
            user = self._session()
            if user is None:
                if path.startswith("/api/"):
                    return self._send(*_json({"error": "sign in first"}, 401))
                self.extra_headers = (("Location", "/login"),)
                return self._send(302, "text/plain; charset=utf-8", b"")
            if path == "/api/me":
                return self._send(*_json({"user": user["user"], "role": user["role"], "csrf": user["csrf"],
                                          "ui_approvals": config.UI_APPROVALS, "services": sorted(config.CRITICALITY),
                                          "signals": list(actions.SIGNALS)}))
            self._send(*route(self.path, user))
        except Exception as e:  # never show a traceback; the page keeps polling
            self._send(*_json({"error": f"internal error: {type(e).__name__}"}, 500))

    def _body(self):
        """The JSON object sent, or None if the request is not acceptable."""
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return None
        if not 0 <= length <= MAX_BODY or not self.headers.get("Content-Type", "").startswith("application/json"):
            return None
        try:
            found = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return None
        return found if isinstance(found, dict) else None

    def do_POST(self):
        self.extra_headers = ()
        origin = self.headers.get("Origin")
        if not self._host_ok() or (origin is not None and origin.removeprefix("http://") not in self._hosts()):
            return self._send(*_json({"error": "bad origin"}, 403))
        body = self._body()
        if body is None:
            return self._send(*_json({"error": "bad request"}, 400))
        path = urlsplit(self.path).path
        try:
            if path == "/api/login":
                return self._login(body)
            user = self._session()
            if user is None:
                return self._send(*_json({"error": "sign in first"}, 401))
            if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), user["csrf"]):
                audit(user["user"], path, 403, {"reason": "bad csrf token"})
                return self._send(*_json({"error": "bad token, reload the page"}, 403))
            if path == "/api/logout":
                sessions.drop(self._token())
                audit(user["user"], "logout", 200)
                self.extra_headers = (("Set-Cookie", f"{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"),)
                return self._send(*_json({"ok": True}))
            found = actions.ROUTES.get(path)
            if found is None:
                return self._send(*_json({"error": "not found"}, 404))
            act, needed = found
            if not auth.can(user["role"], needed):
                audit(user["user"], path, 403, body)
                return self._send(*_json({"error": f"this needs the {needed} role"}, 403))
            status, result = act(user, body)
            audit(user["user"], path, status, body)
            self._send(*_json(result, status))
        except Exception as e:  # never show a traceback
            self._send(*_json({"error": f"internal error: {type(e).__name__}"}, 500))

    def _login(self, body):
        name, password = body.get("name"), body.get("password")
        key = name if isinstance(name, str) else ""
        if throttle.locked(key):
            audit(key[:40], "login", 429)
            return self._send(*_json({"error": "too many attempts, wait a minute"}, 429))
        if not auth.check_password(name, password):
            throttle.failed(key)
            audit(key[:40], "login", 401)
            return self._send(*_json({"error": "wrong name or password"}, 401))
        throttle.passed(key)
        audit(name, "login", 200)
        self.extra_headers = (("Set-Cookie", f"{COOKIE}={sessions.create(name)}; HttpOnly; SameSite=Strict; Path=/; "
                                             f"Max-Age={auth.SESSION_MAX_S}"),)
        return self._send(*_json({"ok": True}))

    def _refuse(self):
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_PUT = do_DELETE = do_PATCH = _refuse

    def log_message(self, fmt, *args):
        pass


def make_server(host="127.0.0.1", port=None):
    if not ipaddress.ip_address(host).is_loopback:
        raise ValueError(f"refusing to bind {host}: the dashboard listens on localhost only")
    return ThreadingHTTPServer((host, config.UI_PORT if port is None else port), Handler)
