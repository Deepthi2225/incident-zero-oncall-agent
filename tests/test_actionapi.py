"""Our own Action API (actionapi/): refusals, token, cooldown, idempotency, dry run, 501s, audit log,
and the agent's HTTP client talking to the real server in-process (fake docker and Locust)."""
import json
import threading

import pytest

from actionapi import config as api_config
from actionapi.server import make_server
from actionapi.service import ActionError, ActionService, LocustClient

TOKEN = "t0k3n-for-tests"


class FakeDocker:
    def __init__(self):
        names = api_config.RESTART_ALLOWLIST | api_config.RESTART_DENYLIST | {"faultctl", "postgres-other"}
        self.containers = {n: {"running": n != "payment", "project": "opentelemetry-demo", "restarts": 0}
                           for n in names}
        self.containers["postgres-other"]["project"] = "some-other-project"
        self.restarts = []

    def inspect(self, name):
        c = self.containers.get(name)
        if c is None:
            raise ActionError(404, f"no such container: {name}")
        return {"State": {"Status": "running" if c["running"] else "exited", "Running": c["running"],
                          "Health": {"Status": "healthy" if c["running"] else "unhealthy"},
                          "StartedAt": "2026-10-24T09:00:00Z"},
                "RestartCount": c["restarts"],
                "Config": {"Labels": {"com.docker.compose.project": c["project"]}}}

    def restart(self, name):
        self.restarts.append(name)
        self.containers[name].update(running=True, restarts=self.containers[name]["restarts"] + 1)


class FakeLocust:
    def __init__(self, users=10):
        self.n, self.set_calls = users, []

    def users(self):
        return self.n, "running"

    def set_users(self, users):
        self.set_calls.append(users)
        self.n = users


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def api(tmp_path):
    svc = ActionService(token=TOKEN, docker=FakeDocker(), locust=FakeLocust(), clock=Clock(),
                        audit_log=tmp_path / "action_api.jsonl", cooldown_s=60)
    svc.requests = 0
    return svc


def call(svc, path, body=None, token=TOKEN, key=None, method="POST", caller=None):
    svc.requests += 1
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if key:
        headers["Idempotency-Key"] = key
    if caller:
        headers["X-Caller"] = caller
    return svc.handle(method, path, headers, json.dumps(body or {}).encode(), caller="127.0.0.1:5555")


def audit(svc):
    return [json.loads(line) for line in svc.audit_log.read_text().splitlines()]


# --- restart_service --------------------------------------------------------------------------------

def test_restart_an_allowlisted_application_service(api):
    status, resp = call(api, "/actions/restart_service", {"service": "payment"}, caller="oncall-agent")
    assert status == 200 and resp["result"] == "restarted" and api.docker.restarts == ["payment"]
    assert resp["before"]["running"] is False and resp["container"] == {
        "status": "running", "running": True, "health": "healthy", "restart_count": 1,
        "started_at": "2026-10-24T09:00:00Z"}
    entry = audit(api)[-1]
    assert (entry["caller"], entry["action"], entry["params"], entry["status"], entry["result"]) == \
        ("oncall-agent", "restart_service", {"service": "payment"}, 200, "restarted")


@pytest.mark.parametrize("name", ["prometheus", "jaeger", "opensearch", "grafana", "otel-collector",
                                  "opamp-server", "flagd", "flagd-ui", "load-generator", "faultctl",
                                  "my-faultctl-loop", "postgres-other", "nginx-unknown"])
def test_restart_refuses_everything_outside_the_allowlist(api, name):
    status, resp = call(api, "/actions/restart_service", {"service": name})
    assert status == 403 and resp["ok"] is False and resp["error"].startswith("refused")
    assert api.docker.restarts == [] and audit(api)[-1]["status"] == 403


def test_denylist_wins_even_if_someone_adds_to_the_allowlist(api, monkeypatch):
    monkeypatch.setattr(api_config, "RESTART_ALLOWLIST", api_config.RESTART_ALLOWLIST | {"prometheus"})
    assert call(api, "/actions/restart_service", {"service": "prometheus"})[0] == 403


@pytest.mark.parametrize("body", [{}, {"service": "../payment"}, {"service": 3}, {"service": "Payment X"}])
def test_bad_service_names_are_rejected(api, body):
    assert call(api, "/actions/restart_service", body)[0] == 400
    assert api.docker.restarts == []


# --- token -------------------------------------------------------------------------------------------

def test_token_is_required_on_every_action_call(api):
    assert call(api, "/actions/restart_service", {"service": "payment"}, token=None) == \
        (401, {"ok": False, "action": "restart_service", "error": "missing token"})
    assert call(api, "/actions/restart_service", {"service": "payment"}, token="guess")[1]["error"] == "wrong token"
    assert call(api, "/actions/scale_service", {"service": "payment"}, token=None)[0] == 401
    assert api.docker.restarts == []
    assert call(api, "/health", method="GET", token=None)[0] == 200  # liveness only, no token needed
    assert [e["status"] for e in audit(api)] == [401, 401, 401, 200]


def test_service_refuses_to_start_without_a_token():
    with pytest.raises(ValueError, match="ACTION_API_TOKEN"):
        ActionService(token="", docker=FakeDocker(), locust=FakeLocust())


# --- cooldown, idempotency, dry run ---------------------------------------------------------------------

def test_cooldown_per_service(api):
    assert call(api, "/actions/restart_service", {"service": "payment"})[0] == 200
    status, resp = call(api, "/actions/restart_service", {"service": "payment"})
    assert status == 429 and resp["retry_after_s"] == 60.0
    assert call(api, "/actions/restart_service", {"service": "cart"})[0] == 200  # other services unaffected
    api.clock.t += 61
    assert call(api, "/actions/restart_service", {"service": "payment"})[0] == 200
    assert api.docker.restarts == ["payment", "cart", "payment"]


def test_idempotency_key_prevents_a_double_apply(api):
    first = call(api, "/actions/restart_service", {"service": "payment"}, key="run1:step1:restart_service")
    again = call(api, "/actions/restart_service", {"service": "payment"}, key="run1:step1:restart_service")
    assert first[0] == again[0] == 200 and again[1]["replayed"] is True and "replayed" not in first[1]
    assert api.docker.restarts == ["payment"]  # applied once, even though it is inside the cooldown
    status, resp = call(api, "/actions/restart_service", {"service": "cart"}, key="run1:step1:restart_service")
    assert status == 409 and api.docker.restarts == ["payment"]
    assert [e["replayed"] for e in audit(api)] == [False, True, False]


def test_dry_run_validates_without_acting(api):
    status, resp = call(api, "/actions/restart_service", {"service": "payment", "dry_run": True})
    assert status == 200 and resp["result"] == "would restart" and resp["dry_run"] is True
    assert api.docker.restarts == []
    assert call(api, "/actions/restart_service", {"service": "prometheus", "dry_run": True})[0] == 403
    assert call(api, "/actions/restart_service", {"service": "payment"})[0] == 200  # no cooldown consumed
    assert call(api, "/actions/throttle_load_generator", {"users": 2, "dry_run": True})[1]["users_after"] == 2
    assert api.locust.set_calls == [] and audit(api)[0]["dry_run"] is True


# --- load generator ------------------------------------------------------------------------------------

def test_throttle_records_the_previous_value_and_unthrottle_restores_it(api):
    status, resp = call(api, "/actions/throttle_load_generator", {"users": 2})
    assert status == 200 and (resp["users_before"], resp["users_now"], resp["restore_to"]) == (10, 2, 10)
    assert call(api, "/actions/throttle_load_generator", {"users": 1})[0] == 429  # cooldown
    status, resp = call(api, "/actions/unthrottle_load_generator")  # a rollback is never blocked
    assert status == 200 and resp["users_now"] == 10 and api.locust.set_calls == [2, 10]
    assert call(api, "/actions/unthrottle_load_generator")[0] == 409  # nothing left to restore


@pytest.mark.parametrize("users", ["lots", -1, 1000, True, None])
def test_throttle_validates_users(api, users):
    assert call(api, "/actions/throttle_load_generator", {"users": users})[0] == 400
    assert api.locust.set_calls == []


def test_locust_url_may_not_point_at_flag_services():
    with pytest.raises(ValueError):
        LocustClient("http://localhost:8080/flagservice")
    with pytest.raises(ValueError):
        LocustClient("http://localhost:8013")


# --- 501s, routing, secrets, audit ------------------------------------------------------------------------

def test_flag_toggle_and_scale_are_501(api):
    status, resp = call(api, "/actions/toggle_feature_flag", {"flag": "paymentFailure"})
    assert status == 501 and resp["error"] == "not implemented: pending mentor approval"
    assert "paymentFailure" not in json.dumps(resp) and "paymentFailure" not in api.audit_log.read_text()
    assert call(api, "/actions/scale_service", {"service": "payment"}) == \
        (501, {"ok": False, "action": "scale_service", "error": "not supported on Docker Compose"})


def test_unknown_routes_and_methods(api):
    assert call(api, "/actions/rm_rf")[0] == 404
    assert call(api, "/actions/restart_service", method="GET")[0] == 405


def test_every_request_is_audited_and_nothing_secret_is_returned(api):
    responses = [
        call(api, "/health", method="GET", token=None),
        call(api, "/actions/restart_service", {"service": "payment"}),
        call(api, "/actions/restart_service", {"service": "payment"}),             # cooldown
        call(api, "/actions/restart_service", {"service": "jaeger"}),              # refused
        call(api, "/actions/restart_service", {"service": "cart", "dry_run": True}),
        call(api, "/actions/restart_service", {"service": "cart"}, token="wrong-token-xyz"),
        call(api, "/actions/throttle_load_generator", {"users": 3}),
        call(api, "/actions/toggle_feature_flag", {"flag": "x"}),
        call(api, "/actions/scale_service", {"service": "cart"}),
        call(api, "/nope"),
    ]
    entries = audit(api)
    assert len(entries) == api.requests == len(responses)
    assert [e["status"] for e in entries] == [200, 200, 429, 403, 200, 401, 200, 501, 501, 404]
    for e in entries:
        assert set(e) >= {"ts", "caller", "action", "params", "status", "result", "duration_ms", "dry_run"}
    text = api.audit_log.read_text() + json.dumps([r for _, r in responses])
    assert TOKEN not in text and "wrong-token-xyz" not in text  # tokens are never logged or echoed


def test_server_binds_localhost_only(api):
    with pytest.raises(ValueError, match="localhost only"):
        make_server(api, host="0.0.0.0", port=0)


# --- integration: the agent's HTTP client against the real server in-process ----------------------------

@pytest.fixture
def live_api(api, mem):
    server = make_server(api, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield api, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_agent_http_client_talks_to_the_real_server(live_api):
    from oncall.action_api import ActionApiError, HttpActionClient
    api, url = live_api
    client = HttpActionClient(url, token=TOKEN)
    res = client.call("restart_service", "payment", idempotency_key="r1:step1:restart_service")
    assert res["status"] == 200 and res["response"]["result"] == "restarted"
    again = client.call("restart_service", "payment", idempotency_key="r1:step1:restart_service")
    assert again["response"]["replayed"] is True and api.docker.restarts == ["payment"]
    with pytest.raises(ActionApiError, match="HTTP 401"):
        HttpActionClient(url, token="wrong").call("restart_service", "cart")
    with pytest.raises(ActionApiError, match="HTTP 403"):
        client.call("restart_service", "prometheus")
    with pytest.raises(ActionApiError, match="HTTP 501"):
        client.call("toggle_feature_flag", "ad", flag="adHighCpu")
    assert [e["caller"] for e in audit(api)][0] == "oncall-agent"


def test_plan_executes_and_rolls_back_through_the_real_server(live_api):
    from oncall.action_api import HttpActionClient
    from oncall.remediate import run_plan
    from oncall.verify import NOT_RECOVERED, RECOVERED
    from tests.test_remediate import StubVerifier, human, logged, plan_for
    api, url = live_api
    diag = plan_for(("payment", "container_stopped_or_restarting"), ("frontend-proxy", "traffic_flood"))
    verifier = StubVerifier({"payment": RECOVERED, "load-generator": NOT_RECOVERED})
    plan = run_plan(diag, HttpActionClient(url, token=TOKEN), human({}), verifier, "run9", log=lambda s: None)
    assert [s["outcome"] for s in plan["steps"]] == ["recovered", "not_recovered"]
    assert api.docker.restarts == ["payment"] and api.locust.set_calls == [2, 10]  # throttled, then restored
    assert plan["steps"][1]["rollback_result"]["status"] == "done"
    assert [e["event"] for e in logged()] == ["call", "verify", "call", "verify", "rollback"]
    assert [(e["action"], e["status"], e["idempotency_key"]) for e in audit(api)] == [
        ("restart_service", 200, "run9:step1:restart_service"),
        ("throttle_load_generator", 200, "run9:step2:throttle_load_generator"),
        ("unthrottle_load_generator", 200, "run9:step2:rollback")]
