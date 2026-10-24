"""The Action API's logic, independent of HTTP so it can be tested with fakes.

    ActionService.handle(method, path, headers, body) -> (http_status, response_dict)

Every request passes, in order: route -> token -> parse -> idempotency replay -> validation
(allowlist, payload) -> dry run -> cooldown -> act -> record. Every request, whatever the result,
is appended to the audit log (runs/action_api.jsonl). Responses carry only success/failure and
container or load-generator state: never flag values, tokens or other secrets.
"""
import hashlib
import hmac
import json
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from actionapi import config


class ActionError(Exception):
    """A refusal or failure with its HTTP status."""

    def __init__(self, status, error, **extra):
        super().__init__(error)
        self.status, self.error, self.extra = status, error, extra


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- the outside world (both replaceable in tests) --------------------------------------------------

class DockerCLI:
    """The only state-changing docker command is `docker restart`, used for allowlisted services."""

    def run(self, *args):
        out = subprocess.run(["docker", *args], capture_output=True, text=True,
                             timeout=config.DOCKER_TIMEOUT_S, encoding="utf-8", errors="replace")
        return out.returncode, out.stdout, out.stderr

    def inspect(self, name):
        rc, out, err = self.run("inspect", name)
        if rc != 0:
            raise ActionError(404, f"no such container: {name}")
        return json.loads(out)[0]

    def restart(self, name):
        rc, _, err = self.run("restart", "--time", "10", name)
        if rc != 0:
            raise ActionError(502, f"docker restart {name} failed: {err.strip()[:200]}")


class LocustClient:
    """Locust's own web API: GET /stats/requests (current user_count), POST /swarm (set users)."""

    def __init__(self, base=None):
        self.base = (base or config.LOCUST_URL).rstrip("/")
        if any(bad in self.base.lower() for bad in config.FORBIDDEN_URL_SUBSTRINGS):
            raise ValueError(f"refusing Locust URL {self.base}: feature-flag related")

    def _request(self, path, data=None):
        req = urllib.request.Request(self.base + path, method="POST" if data is not None else "GET",
                                     data=urllib.parse.urlencode(data).encode() if data is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT_S) as resp:
                return json.loads(resp.read().decode() or "{}")
        except (urllib.error.URLError, ValueError, OSError) as e:
            raise ActionError(502, f"load generator API failed: {str(e)[:200]}")

    def users(self):
        stats = self._request("/stats/requests")
        return int(stats.get("user_count", 0)), stats.get("state")

    def set_users(self, users):
        self._request("/swarm", {"user_count": users, "spawn_rate": max(users, 1)})


# --- the service ---------------------------------------------------------------------------------------

ROUTES = {
    ("GET", "/health"): "health",
    ("POST", "/actions/restart_service"): "restart_service",
    ("POST", "/actions/throttle_load_generator"): "throttle_load_generator",
    ("POST", "/actions/unthrottle_load_generator"): "unthrottle_load_generator",
    ("POST", "/actions/toggle_feature_flag"): "toggle_feature_flag",
    ("POST", "/actions/scale_service"): "scale_service",
}
NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")


class ActionService:
    def __init__(self, token=None, docker=None, locust=None, clock=time.time, audit_log=None,
                 cooldown_s=None, kill_switch=None):
        self.token = config.TOKEN if token is None else token
        if not self.token:
            raise ValueError("ACTION_API_TOKEN is not set; refusing to start without a shared token")
        self.docker = docker or DockerCLI()
        self.locust = locust or LocustClient()
        self.clock = clock
        self.audit_log = audit_log or config.AUDIT_LOG
        self.cooldown_s = config.COOLDOWN_S if cooldown_s is None else cooldown_s
        self.kill_switch = Path(kill_switch or config.KILL_SWITCH_FILE)
        self.last_action = {}      # cooldown key -> time of the last applied action
        self.idempotency = {}      # key -> (request fingerprint, status, response, time)
        self.previous_users = None  # load-generator users before the first throttle
        self.lock = threading.Lock()

    # --- entry point ------------------------------------------------------------------------------
    def handle(self, method, path, headers, body, caller="unknown"):
        started = time.time()
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        caller = headers.get("x-caller") or caller
        route = path.split("?")[0]
        action, params, dry_run, key = ROUTES.get((method, route)), {}, False, None
        try:
            if action is None:
                known = any(p == route for _, p in ROUTES)
                raise ActionError(405 if known else 404, f"no route {method} {route}")
            if action != "health":
                self._authorise(headers)
            params = self._parse(body) if method == "POST" else {}
            dry_run = bool(params.pop("dry_run", False)) or "dry_run=1" in path
            key = headers.get("idempotency-key") or params.pop("idempotency_key", None)
            if action != "health" and self.kill_switch.exists():
                # Refuses everything, dry runs and idempotent replays included (based on Padma's
                # checkpoint3 branch: the API itself enforces the kill switch).
                raise ActionError(423, "kill switch is on")
            with self.lock:
                status, response = self._dispatch(action, params, dry_run, key)
        except ActionError as e:
            status, response = e.status, {"ok": False, "action": action, "error": e.error, **e.extra}
        except Exception as e:  # never leak a traceback; log the failure
            status, response = 500, {"ok": False, "action": action, "error": f"internal error: {type(e).__name__}"}
        self._audit(caller, method, path, action, params, dry_run, key, status, response,
                    round((time.time() - started) * 1000, 1))
        return status, response

    # --- checks -----------------------------------------------------------------------------------
    def _authorise(self, headers):
        auth = headers.get("authorization", "")
        if not auth:
            raise ActionError(401, "missing token")
        given = auth.removeprefix("Bearer ").strip()
        if not hmac.compare_digest(given.encode(), self.token.encode()):
            raise ActionError(401, "wrong token")

    @staticmethod
    def _parse(body):
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            raise ActionError(400, "body must be JSON")
        if not isinstance(data, dict):
            raise ActionError(400, "body must be a JSON object")
        return data

    def _check_service(self, name):
        if not isinstance(name, str) or not NAME.match(name):
            raise ActionError(400, "service must be a container name")
        lowered = name.lower()
        if name in config.RESTART_DENYLIST or any(bad in lowered for bad in config.DENY_SUBSTRINGS):
            raise ActionError(403, f"refused: {name} is never restarted by the Action API")
        if name not in config.RESTART_ALLOWLIST:
            raise ActionError(403, f"refused: {name} is not an allowlisted application service")

    def _cooldown(self, key):
        last = self.last_action.get(key)
        if last is not None:
            left = self.cooldown_s - (self.clock() - last)
            if left > 0:
                raise ActionError(429, f"cooldown: {key} was changed {self.cooldown_s - left:.0f}s ago",
                                  retry_after_s=round(left, 1))

    # --- dispatch with idempotency -------------------------------------------------------------------
    def _dispatch(self, action, params, dry_run, key):
        if action == "health":
            return 200, {"ok": True, "service": "actionapi", "time": _now_iso()}
        fingerprint = hashlib.sha256(json.dumps([action, params, dry_run], sort_keys=True).encode()).hexdigest()
        if key:
            self._expire()
            seen = self.idempotency.get(key)
            if seen:
                if seen[0] != fingerprint:
                    raise ActionError(409, "idempotency key reused with a different request")
                return seen[1], {**seen[2], "replayed": True}
        status, response = getattr(self, f"_{action}")(params, dry_run)
        if key and not dry_run:
            self.idempotency[key] = (fingerprint, status, response, self.clock())
        return status, response

    def _expire(self):
        now = self.clock()
        for k in [k for k, v in self.idempotency.items() if now - v[3] > config.IDEMPOTENCY_TTL_S]:
            del self.idempotency[k]

    # --- actions -------------------------------------------------------------------------------------
    def _container_state(self, info):
        st = info.get("State", {})
        return {"status": st.get("Status"), "running": bool(st.get("Running")),
                "health": (st.get("Health") or {}).get("Status", "none"),
                "restart_count": info.get("RestartCount", 0), "started_at": st.get("StartedAt")}

    def _restart_service(self, params, dry_run):
        name = params.get("service")
        self._check_service(name)
        info = self.docker.inspect(name)
        project = (info.get("Config", {}).get("Labels") or {}).get("com.docker.compose.project")
        if project != config.COMPOSE_PROJECT:
            raise ActionError(403, f"refused: {name} is not part of the {config.COMPOSE_PROJECT} project")
        before = self._container_state(info)
        if dry_run:
            self._cooldown(name)
            return 200, {"ok": True, "action": "restart_service", "service": name, "dry_run": True,
                         "result": "would restart", "container": before}
        self._cooldown(name)
        self.docker.restart(name)
        self.last_action[name] = self.clock()
        after = self._container_state(self.docker.inspect(name))
        return 200, {"ok": True, "action": "restart_service", "service": name, "dry_run": False,
                     "result": "restarted", "container": after, "before": before}

    def _throttle_load_generator(self, params, dry_run):
        users = params.get("users")
        if not isinstance(users, int) or isinstance(users, bool) or not 0 <= users <= config.LOCUST_MAX_USERS:
            raise ActionError(400, f"users must be an integer from 0 to {config.LOCUST_MAX_USERS}")
        current, state = self.locust.users()
        self._cooldown("load-generator")
        if dry_run:
            return 200, {"ok": True, "action": "throttle_load_generator", "dry_run": True,
                         "result": "would throttle", "users_now": current, "users_after": users}
        if self.previous_users is None:  # remember the value before the FIRST throttle
            self.previous_users = current
        self.locust.set_users(users)
        self.last_action["load-generator"] = self.clock()
        return 200, {"ok": True, "action": "throttle_load_generator", "dry_run": False, "result": "throttled",
                     "users_before": current, "users_now": users, "restore_to": self.previous_users}

    def _unthrottle_load_generator(self, params, dry_run):
        """The inverse of a throttle. Exempt from the cooldown: a rollback must never be blocked."""
        if self.previous_users is None:
            raise ActionError(409, "nothing to restore: the load generator was not throttled through this API")
        current, _ = self.locust.users()
        if dry_run:
            return 200, {"ok": True, "action": "unthrottle_load_generator", "dry_run": True,
                         "result": "would restore", "users_now": current, "users_after": self.previous_users}
        restore, self.previous_users = self.previous_users, None
        self.locust.set_users(restore)
        self.last_action["load-generator"] = self.clock()
        return 200, {"ok": True, "action": "unthrottle_load_generator", "dry_run": False,
                     "result": "restored", "users_before": current, "users_now": restore}

    def _toggle_feature_flag(self, params, dry_run):
        # Never reads or writes flag files or the flag API (HARD RULE 2). The flag name is not echoed.
        raise ActionError(501, "not implemented: pending mentor approval")

    def _scale_service(self, params, dry_run):
        raise ActionError(501, "not supported on Docker Compose")

    # --- audit ------------------------------------------------------------------------------------------
    def _audit(self, caller, method, path, action, params, dry_run, key, status, response, ms):
        entry = {"ts": _now_iso(), "caller": caller, "method": method, "path": path.split("?")[0],
                 "action": action, "params": {k: v for k, v in params.items() if k in ("service", "users")},
                 "dry_run": dry_run, "idempotency_key": key, "status": status,
                 "result": response.get("result") or response.get("error"),
                 "replayed": bool(response.get("replayed")), "duration_ms": ms}
        self.audit_log.parent.mkdir(parents=True, exist_ok=True)
        with open(self.audit_log, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
