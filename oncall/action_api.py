"""Action API clients: the ONLY way the agent changes the system (HARD RULE 1).

Every endpoint path and payload lives in ENDPOINTS below. They are PLACEHOLDERS until the
organisers publish the spec; replacing them should be the only change needed here.

    HttpActionClient  real HTTP calls to ACTION_API_URL (live=True)
    DryRunClient      builds the exact request it would send and sends nothing (live=False)
    FakeClient        for tests: records calls, can fail on demand (live configurable)

Clients do not decide anything: approval, kill switch and logging happen in oncall/remediate.py.
"""
import requests

from oncall import config
from oncall.agent.actions import ACTIONS
from oncall.sources.guard import check

# action -> (HTTP method, path, payload builder), matching our own Action API (actionapi/, built on
# the mentors' instruction). A path may depend on params: the throttle's inverse ({"remove": True})
# goes to the unthrottle endpoint. A builder gets (target, flag, params).
ENDPOINTS = {
    "restart_service": ("POST", "/actions/restart_service",
                        lambda target, flag, params: {"service": target}),
    "toggle_feature_flag": ("POST", "/actions/toggle_feature_flag",
                            lambda target, flag, params: {"flag": flag}),
    "throttle_load_generator": ("POST", lambda params: "/actions/unthrottle_load_generator"
                                if params.get("remove") else "/actions/throttle_load_generator",
                                lambda target, flag, params: {} if params.get("remove")
                                else {"users": int(params.get("users", config.THROTTLE_USERS))}),
    "scale_service": ("POST", "/actions/scale_service",
                      lambda target, flag, params: {"service": target}),
}


class ActionApiError(RuntimeError):
    pass


class KillSwitchRefused(ActionApiError):
    """HTTP 423: the Action API's own kill switch is on. A refusal, never retried.
    (The API-side kill switch is based on Padma's checkpoint3 branch.)"""


def build_request(action, target=None, flag=None, params=None, base_url=None):
    """The request for one action: {method, url, json}. Refuses actions outside the allowlist."""
    if action not in ACTIONS:
        raise ActionApiError(f"{action} is not an Action API action")
    if not ACTIONS[action]["available"]:
        raise ActionApiError(f"{action} is unavailable: {ACTIONS[action]['unavailable_because']}")
    method, path, payload = ENDPOINTS[action]
    path = path(params or {}) if callable(path) else path
    url = (base_url or config.ACTION_API_URL or "<ACTION_API_URL>").rstrip("/") + path
    return {"method": method, "url": url, "json": payload(target, flag, params or {})}


class HttpActionClient:
    live = True

    def __init__(self, base_url=None, token=None, timeout=None):
        self.base_url = base_url or config.ACTION_API_URL
        if not self.base_url:
            raise ActionApiError("ACTION_API_URL is not set; use the dry-run client")
        self.token = token if token is not None else config.ACTION_API_TOKEN
        self.timeout = timeout or config.ACTION_API_TIMEOUT
        self.name = f"http {self.base_url}"

    def call(self, action, target=None, flag=None, params=None, idempotency_key=None):
        req = build_request(action, target, flag, params, self.base_url)
        check(req["url"])  # never a flag service URL (HARD RULE 2)
        headers = {"X-Caller": "oncall-agent"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        for attempt in (1, 2):  # one retry on a connection error; the idempotency key prevents a double apply
            try:
                resp = requests.request(req["method"], req["url"], json=req["json"], headers=headers,
                                        timeout=self.timeout)
                break
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt == 2 or not idempotency_key:
                    raise ActionApiError(f"{req['method']} {req['url']} failed: {e}") from e
            except requests.RequestException as e:
                raise ActionApiError(f"{req['method']} {req['url']} failed: {e}") from e
        try:
            body = resp.json()
        except ValueError:
            body = resp.text[:500]
        if resp.status_code == 423:
            raise KillSwitchRefused("refused: kill switch is on (Action API)")
        if not resp.ok:
            raise ActionApiError(f"{req['method']} {req['url']} -> HTTP {resp.status_code}: {str(body)[:300]}")
        return {"request": req, "status": resp.status_code, "response": body}


class DryRunClient:
    live = False
    name = "dry-run"

    def call(self, action, target=None, flag=None, params=None, idempotency_key=None):
        req = build_request(action, target, flag, params)
        return {"request": req, "status": None, "response": "dry run: nothing sent"}


class FakeClient:
    """Test double. fail: set of actions that raise ActionApiError."""

    def __init__(self, live=True, fail=()):
        self.live = live
        self.fail = set(fail)
        self.calls, self.keys = [], []
        self.name = "fake"

    def call(self, action, target=None, flag=None, params=None, idempotency_key=None):
        req = build_request(action, target, flag, params, "http://fake")
        self.keys.append(idempotency_key)
        self.calls.append((action, target, flag, params or {}))
        if action in self.fail:
            raise ActionApiError(f"fake failure for {action}")
        return {"request": req, "status": 200, "response": {"ok": True}}
