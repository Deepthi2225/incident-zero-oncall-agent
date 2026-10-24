"""Day 2: Action API clients, the step-by-step executor, verify, rollback, action log, report."""
import json

import pytest

from oncall import config
from oncall.action_api import (ActionApiError, DryRunClient, FakeClient, HttpActionClient,
                               build_request)
from oncall.agent.diagnosis import validate
from oncall.evidence import registry
from oncall.remediate import make_approver, run_plan
from oncall.report import build_report
from oncall.sources.guard import ForbiddenAccess
from oncall.verify import (INCONCLUSIVE, NOT_RECOVERED, PARTIAL, RECOVERED, Verifier)


@pytest.fixture(autouse=True)
def clean(mem):  # mem: in-memory source, tmp ACTION_LOG / BASELINE_FILE / KILL_SWITCH_FILE
    registry.reset()
    yield


def logged():
    if not config.ACTION_LOG.exists():
        return []
    return [json.loads(line) for line in config.ACTION_LOG.read_text().splitlines()]


def plan_for(*rcs):
    root_causes = []
    for component, fault_class in rcs:
        eid = registry.add("t", "container", f"{component} evidence")
        root_causes.append({"component": component, "fault_class": fault_class, "cause": "c",
                            "confidence": 0.9, "evidence": [eid]})
    return validate({"headline": "h", "affected_services": [], "anomalies": [], "open_questions": [],
                     "root_causes": root_causes, "proposed_action": {}})


class StubVerifier:
    """Returns scripted outcomes per step target; records the order of calls."""

    def __init__(self, outcomes, unhealthy=None):
        self.outcomes, self.unhealthy, self.calls = outcomes, unhealthy or {}, []

    def measure(self, services):
        return {s: {"rps": 0.0, "error_rate": 1.0, "p95_ms": None} for s in services}

    def verify(self, step, services, before=None, progress=None):
        self.calls.append(("verify", step["target"]))
        return {"outcome": self.outcomes[step["target"]], "checks": [], "services": {},
                "unhealthy": self.unhealthy.get(step["target"], []), "evidence": []}


def human(answers, calls=None):
    """Approver that answers per step like a human typing y/N."""
    def approve(step, diag):
        if calls is not None:
            calls.append(("approve", step["target"]))
        ok = answers.get(step["target"], True)
        return {"status": "approved" if ok else "rejected", "approved_by": "human" if ok else None}
    return approve


# --- Action API clients ----------------------------------------------------------------------------

def test_requests_are_built_from_one_endpoint_table():
    req = build_request("restart_service", "payment", base_url="http://api")
    assert req == {"method": "POST", "url": "http://api/actions/restart_service", "json": {"service": "payment"}}
    assert build_request("throttle_load_generator", "load-generator", base_url="http://api") == {
        "method": "POST", "url": "http://api/actions/throttle_load_generator", "json": {"users": 2}}
    # the inverse (rollback) goes to the unthrottle endpoint
    assert build_request("throttle_load_generator", "load-generator", params={"remove": True},
                         base_url="http://api") == {
        "method": "POST", "url": "http://api/actions/unthrottle_load_generator", "json": {}}
    with pytest.raises(ActionApiError, match="unavailable"):
        build_request("scale_service", "payment")
    with pytest.raises(ActionApiError, match="not an Action API action"):
        build_request("docker_restart", "payment")


def test_dry_run_client_sends_nothing(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "request", lambda *a, **k: pytest.fail("network used in dry run"))
    res = DryRunClient().call("restart_service", "payment")
    assert res["request"]["json"] == {"service": "payment"} and "dry run" in res["response"]


def test_http_client(monkeypatch):
    import requests
    monkeypatch.setattr(config, "ACTION_API_URL", "")
    with pytest.raises(ActionApiError, match="ACTION_API_URL"):
        HttpActionClient()
    sent = []

    class Resp:
        ok, status_code, text = True, 200, ""

        def json(self):
            return {"accepted": True}

    def fake_request(method, url, json=None, headers=None, timeout=None):
        sent.append((method, url, json, headers))
        return Resp()

    monkeypatch.setattr(requests, "request", fake_request)
    res = HttpActionClient("http://api", token="t").call("restart_service", "payment")
    assert sent == [("POST", "http://api/actions/restart_service", {"service": "payment"},
                     {"X-Caller": "oncall-agent", "Authorization": "Bearer t"})]
    assert res["status"] == 200 and res["response"] == {"accepted": True}

    Resp.ok, Resp.status_code = False, 500
    with pytest.raises(ActionApiError, match="HTTP 500"):
        HttpActionClient("http://api").call("restart_service", "payment")
    with pytest.raises(ForbiddenAccess):  # an Action API on a flag service URL is refused
        HttpActionClient("http://localhost:8080/flagservice").call("restart_service", "payment")


# --- executor ---------------------------------------------------------------------------------------

def test_each_step_is_approved_acted_and_verified_before_the_next():
    diag = plan_for(("payment", "container_stopped_or_restarting"), ("valkey-cart", "dependency_down"))
    order, client = [], FakeClient()
    verifier = StubVerifier({"valkey-cart": RECOVERED, "payment": RECOVERED})
    verifier.calls = order
    plan = run_plan(diag, client, human({}, order), verifier, "r1", log=lambda s: None)
    assert plan["outcome"] == "recovered" and plan["escalate"] is False
    assert order == [("approve", "valkey-cart"), ("verify", "valkey-cart"),
                     ("approve", "payment"), ("verify", "payment")]
    assert [c[:2] for c in client.calls] == [("restart_service", "valkey-cart"), ("restart_service", "payment")]
    events = [(e["event"], e["target"]) for e in logged()]
    assert events == [("call", "valkey-cart"), ("verify", "valkey-cart"), ("call", "payment"), ("verify", "payment")]
    assert all(e["run_id"] == "r1" for e in logged())


def test_auto_approval_never_executes_on_a_live_client():
    diag = plan_for(("payment", "container_stopped_or_restarting"))
    client = FakeClient(live=True)
    plan = run_plan(diag, client, make_approver(auto_approve=True), StubVerifier({}), log=lambda s: None)
    assert client.calls == [] and plan["outcome"] == "stopped"
    ex = plan["steps"][0]["execution"]
    assert (ex["status"], ex["reason"]) == ("refused", "auto-approval is dry-run only") and ex["at"]
    assert plan["steps"][0]["outcome"] == "refused"
    assert logged()[0]["event"] == "refused"


def test_auto_approval_runs_the_dry_run_client():
    diag = plan_for(("payment", "container_stopped_or_restarting"), ("valkey-cart", "dependency_down"))
    plan = run_plan(diag, DryRunClient(), make_approver(auto_approve=True), StubVerifier({}), log=lambda s: None)
    assert plan["outcome"] == "dry_run"
    assert [s["execution"]["status"] for s in plan["steps"]] == ["dry_run", "dry_run"]
    assert plan["steps"][0]["verify"]["outcome"] == "skipped"
    assert [e["event"] for e in logged()] == ["dry_run", "dry_run"]


def test_rejected_step_stops_the_plan_and_escalates_the_rest():
    diag = plan_for(("payment", "container_stopped_or_restarting"), ("valkey-cart", "dependency_down"))
    client = FakeClient()
    plan = run_plan(diag, client, human({"valkey-cart": False}), StubVerifier({}), log=lambda s: None)
    assert client.calls == [] and plan["outcome"] == "stopped" and plan["escalate"]
    assert [n["target"] for n in plan["not_run"]] == ["payment"]
    assert logged()[0]["event"] == "refused"


def test_kill_switch_before_the_plan_and_right_before_the_action():
    diag = plan_for(("payment", "container_stopped_or_restarting"))
    config.KILL_SWITCH_FILE.write_text("stop")
    client, asked = FakeClient(), []
    plan = run_plan(diag, client, human({}, asked), StubVerifier({}), log=lambda s: None)
    assert client.calls == [] and asked == [] and plan["stopped_reason"] == "kill switch is on"
    config.KILL_SWITCH_FILE.unlink()

    # Switched on while the human was approving: still nothing is sent.
    diag = plan_for(("payment", "container_stopped_or_restarting"))

    def approve_then_kill(step, d):
        config.KILL_SWITCH_FILE.write_text("stop")
        return {"status": "approved", "approved_by": "human"}

    plan = run_plan(diag, client, approve_then_kill, StubVerifier({}), log=lambda s: None)
    assert client.calls == [] and plan["steps"][0]["execution"]["reason"] == "kill switch is on"


def test_not_recovered_rolls_back_when_an_inverse_exists_then_escalates():
    diag = plan_for(("frontend-proxy", "traffic_flood"), ("payment", "container_stopped_or_restarting"))
    assert [s["target"] for s in diag["remediation_plan"]["steps"]] == ["payment", "load-generator"]
    client = FakeClient()
    verifier = StubVerifier({"payment": RECOVERED, "load-generator": NOT_RECOVERED})
    plan = run_plan(diag, client, human({}), verifier, log=lambda s: None)
    assert client.calls[-1] == ("throttle_load_generator", "load-generator", None, {"remove": True})
    step = plan["steps"][1]
    assert step["rollback_result"]["status"] == "done" and plan["outcome"] == "stopped" and plan["escalate"]
    assert [e["event"] for e in logged()][-1] == "rollback"


def test_not_recovered_restart_has_nothing_to_roll_back():
    diag = plan_for(("valkey-cart", "dependency_down"), ("payment", "container_stopped_or_restarting"))
    client = FakeClient()
    plan = run_plan(diag, client, human({}), StubVerifier({"valkey-cart": NOT_RECOVERED}), log=lambda s: None)
    assert len(client.calls) == 1 and plan["steps"][0]["rollback_result"]["status"] == "none"
    assert [n["target"] for n in plan["not_run"]] == ["payment"]


def test_kill_switch_blocks_the_rollback_too():
    diag = plan_for(("frontend-proxy", "traffic_flood"))
    client = FakeClient()

    class KillingVerifier(StubVerifier):
        def verify(self, step, services, before=None, progress=None):
            config.KILL_SWITCH_FILE.write_text("stop")
            return super().verify(step, services, before)

    plan = run_plan(diag, client, human({}), KillingVerifier({"load-generator": NOT_RECOVERED}), log=lambda s: None)
    assert len(client.calls) == 1  # the action, but no rollback
    assert plan["steps"][0]["rollback_result"]["status"] == "refused"
    assert logged()[-1]["event"] == "rollback_refused"


def test_failed_call_is_logged_and_stops():
    diag = plan_for(("payment", "container_stopped_or_restarting"))
    plan = run_plan(diag, FakeClient(fail={"restart_service"}), human({}), StubVerifier({}), log=lambda s: None)
    assert plan["steps"][0]["execution"]["status"] == "failed" and plan["outcome"] == "stopped"
    assert logged()[0]["event"] == "failed" and "fake failure" in logged()[0]["error"]


def test_partial_continues_only_when_a_later_step_explains_it():
    # cart still unhealthy after restarting payment, but cart depends on valkey-cart (a later step).
    diag = plan_for(("payment", "container_stopped_or_restarting"), ("cart", "container_stopped_or_restarting"))
    steps = diag["remediation_plan"]["steps"]
    assert [s["target"] for s in steps] == ["payment", "cart"]
    client = FakeClient()
    plan = run_plan(diag, client, human({}), StubVerifier({"payment": PARTIAL, "cart": RECOVERED},
                                                          unhealthy={"payment": ["cart"]}), log=lambda s: None)
    assert len(client.calls) == 2 and plan["outcome"] == "recovered"
    assert "later step" in plan["steps"][0]["verify"]["note"]

    # Unexplained partial: stop, keep the step (no rollback), escalate the rest.
    diag = plan_for(("payment", "container_stopped_or_restarting"), ("cart", "container_stopped_or_restarting"))
    client = FakeClient()
    plan = run_plan(diag, client, human({}), StubVerifier({"payment": PARTIAL},  # email: not cart's caller
                                                          unhealthy={"payment": ["email"]}), log=lambda s: None)
    assert len(client.calls) == 1 and plan["outcome"] == "stopped" and "rollback_result" not in plan["steps"][0]


def test_no_steps_means_escalation_only():
    diag = plan_for(("cart", "network_degradation"))
    plan = run_plan(diag, FakeClient(), human({}), StubVerifier({}), log=lambda s: None)
    assert plan["outcome"] == "escalated" and plan["escalate"] and logged() == []


# --- verify -----------------------------------------------------------------------------------------

BASE = {"payment": {"rps": 0.02, "error_rate": 0.0, "p95_ms": 4.0},
        "checkout": {"rps": 0.02, "error_rate": 0.0, "p95_ms": 4000.0}}
HEALTHY = {"payment": {"rps": 0.02, "error_rate": 0.0, "p95_ms": 5.0},
           "checkout": {"rps": 0.02, "error_rate": 0.01, "p95_ms": 5000.0}}
DOWN = {"payment": None, "checkout": {"rps": 0.02, "error_rate": 1.0, "p95_ms": 9000.0}}
HALF = {"payment": HEALTHY["payment"], "checkout": DOWN["checkout"]}


class ScriptedVerifier(Verifier):
    """Real verify loop; measurements and the container state come from a script."""

    def __init__(self, polls, container_ok=True, **kw):
        self.t = 0.0
        kw.setdefault("fast_path", False)
        super().__init__(timeout_s=90, interval_s=30, consecutive=2,
                         sleep=lambda s: setattr(self, "t", self.t + s), clock=lambda: self.t, **kw)
        self.polls, self.container_ok = list(polls), container_ok

    def measure(self, services):
        return {s: DOWN.get(s) for s in services}

    def sample(self, services):  # a new metrics sample on every poll
        snap = self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
        return {s: snap.get(s) for s in services}, 30.0, True

    def serving(self, step, since):  # positive evidence is tested in test_verify.py
        return {"ok": True, "target": step["target"], "server_spans": 1, "client_calls": 0,
                "example_trace": "t1"}

    def container(self, name):
        return {"ok": self.container_ok, "status": "running" if self.container_ok else "exited",
                "health": "healthy" if self.container_ok else "unhealthy"}


@pytest.fixture
def baseline():
    config.BASELINE_FILE.write_text(json.dumps({"source": "mem", "services": BASE}))


STEP = {"action": "restart_service", "target": "payment", "step": 1}


def test_two_consecutive_healthy_checks_mean_recovered(baseline):
    v = ScriptedVerifier([HEALTHY, DOWN, HEALTHY, HEALTHY]).verify(STEP, ["payment", "checkout"], before=DOWN)
    assert v["outcome"] == RECOVERED and [c["ok"] for c in v["checks"]] == [True, False, True, True]
    # every check (and the serving evidence) is registered
    assert all(registry.get(e)["tool"] == "verify" for e in v["evidence"]) and len(v["evidence"]) == 5
    assert len(v["checks"]) == 4 and v["serving"]["server_spans"] == 1
    m = v["services"]["checkout"]
    assert m["baseline"] == BASE["checkout"] and m["before"] == DOWN["checkout"] and m["after"] == HEALTHY["checkout"]


def test_partial_not_recovered_and_inconclusive(baseline):
    v = ScriptedVerifier([HALF]).verify(STEP, ["payment", "checkout"])
    assert v["outcome"] == PARTIAL and v["unhealthy"] == ["checkout"]
    assert any("error rate" in p for p in v["services"]["checkout"]["problems"])
    assert len(v["checks"]) == 4  # 0, 30, 60, 90 s: polled until the timeout

    v = ScriptedVerifier([DOWN], container_ok=False).verify(STEP, ["payment", "checkout"])
    assert v["outcome"] == NOT_RECOVERED

    config.BASELINE_FILE.unlink()  # nothing to compare with, and not a restart: stop at once
    v = ScriptedVerifier([HEALTHY]).verify({**STEP, "action": "throttle_load_generator"}, ["payment"])
    assert v["outcome"] == INCONCLUSIVE and len(v["checks"]) == 1


def test_tolerances(monkeypatch):
    base = {"rps": 1.0, "error_rate": 0.01, "p95_ms": 100.0}
    assert Verifier.judge({"rps": 0.6, "error_rate": 0.02, "p95_ms": 190.0}, base) == (True, [])
    assert Verifier.judge({"rps": 0.4, "error_rate": 0.0, "p95_ms": 100.0}, base)[0] is False
    assert Verifier.judge({"rps": 3.5, "error_rate": 0.0, "p95_ms": 100.0}, base)[0] is False  # flood
    assert Verifier.judge({"rps": 1.0, "error_rate": 0.05, "p95_ms": 100.0}, base)[0] is False
    assert Verifier.judge({"rps": 1.0, "error_rate": 0.0, "p95_ms": 250.0}, base)[0] is False
    monkeypatch.setattr(config, "VERIFY_P95_MAX_RATIO", 3.0)
    assert Verifier.judge({"rps": 1.0, "error_rate": 0.0, "p95_ms": 250.0}, base)[0] is True
    assert Verifier.judge(None, base) == (False, ["no traffic"])
    assert Verifier.judge(None, None)[0] is None


# --- report and end to end -----------------------------------------------------------------------------

def test_report_shows_actions_checks_before_after_and_partial(baseline):
    diag = plan_for(("payment", "container_stopped_or_restarting"))
    verifier = ScriptedVerifier([HALF])
    plan = run_plan(diag, FakeClient(), human({}), verifier, "r9", log=lambda s: None)
    assert plan["steps"][0]["verify"]["outcome"] == PARTIAL
    md = build_report({"run_id": "r9", "summary": diag, "evidence": registry.dump(), "steps": []})
    assert "### Step 1: restart_service on `payment`" in md and "Result: executed" in md
    assert "Verify: **partial**" in md and "| Check | Time | Healthy | Passed | Evidence |" in md
    assert "| Service | Baseline | Before | After | OK |" in md
    assert "Partial recovery, still unhealthy:" in md and "- checkout: error rate" in md
    assert "Plan stopped: step 1 verify: partial." in md


def test_agent_runs_the_plan_after_the_diagnosis(payment_down, monkeypatch):
    from tests.test_agent_loop import block_tool, make_agent, response, submit
    calls = []

    def script(n, kwargs):
        if n == 1:
            return response(block_tool("d", "get_deploy_history", {"lookback": "30m"}))
        eid = json.loads(kwargs["messages"][-1]["content"][0]["content"])["changed_or_unhealthy"][0]["evidence"]
        return response(submit("s", "payment", [eid]))

    agent, _ = make_agent(monkeypatch, script)
    agent.action_client = FakeClient()
    agent.approve = human({}, calls)
    agent.verifier = StubVerifier({"payment": RECOVERED})
    rec = agent.run({"service": "checkout"})
    plan = rec["remediation_plan"]
    assert plan["outcome"] == "recovered" and calls == [("approve", "payment")]
    assert agent.action_client.calls == [("restart_service", "payment", None, {})]
    saved = json.loads(open(rec["path"], encoding="utf-8").read())
    assert saved["remediation_plan"]["steps"][0]["execution"]["status"] == "executed"
    assert saved["outcome"]["status"] == "recovered" and saved["status"] == "complete"
