"""Remaining Day 2 list items, fast verification and verify-only mode."""
import json
from types import SimpleNamespace

import pytest

from oncall import config
from oncall.action_api import FakeClient
from oncall.evidence import registry
from oncall.remediate import run_plan
from oncall.report import build_report
from oncall.verify import (INCONCLUSIVE, NOT_RECOVERED, RECOVERED, Verifier, increases,
                           quantile_from_buckets)
from tests.test_remediate import BASE, STEP, StubVerifier, human, logged, plan_for


@pytest.fixture(autouse=True)
def clean(mem):
    registry.reset()
    yield


@pytest.fixture
def baseline():
    config.BASELINE_FILE.write_text(json.dumps({"source": "mem", "services": BASE}))


# --- gaps from the Day 2 test list ------------------------------------------------------------

@pytest.mark.parametrize("action,reason", [("docker_restart", "not an Action API action"),
                                           ("scale_service", "unavailable")])
def test_executor_refuses_actions_outside_the_allowlist(action, reason):
    diag = plan_for(("payment", "container_stopped_or_restarting"))
    diag["remediation_plan"]["steps"][0]["action"] = action  # e.g. a tampered or hand-edited plan
    client = FakeClient()
    plan = run_plan(diag, client, human({}), StubVerifier({}), log=lambda s: None)
    assert client.calls == [] and plan["outcome"] == "stopped"
    assert reason in plan["steps"][0]["execution"]["reason"]
    assert logged()[0]["event"] == "refused" and reason in logged()[0]["reason"]


def test_inconclusive_verify_stops_without_rollback():
    diag = plan_for(("frontend-proxy", "traffic_flood"))  # throttle has an inverse
    client = FakeClient()
    plan = run_plan(diag, client, human({}), StubVerifier({"load-generator": INCONCLUSIVE}), log=lambda s: None)
    assert len(client.calls) == 1 and "rollback_result" not in plan["steps"][0]
    assert plan["outcome"] == "stopped" and plan["escalate"]


def test_report_for_an_escalation():
    diag = plan_for(("cart", "network_degradation"))
    run_plan(diag, FakeClient(), human({}), StubVerifier({}), log=lambda s: None)
    md = build_report({"run_id": "r2", "summary": diag, "evidence": registry.dump(), "steps": []})
    assert "Plan: **escalated**" in md and "No action was executed on the system." in md
    assert "## Escalated to a human" in md and "**cart** [network_degradation]" in md
    assert "Compare client and server span durations" in md  # next steps for the human
    assert "## What would prevent a repeat" in md and "### Step" not in md


# --- fast verification ---------------------------------------------------------------------------

def test_quantile_from_bucket_deltas():
    assert quantile_from_buckets(0.95, {"10": 50, "100": 90, "1000": 100, "+Inf": 100}) == 550.0
    assert quantile_from_buckets(0.5, {"10": 0, "+Inf": 0}) is None
    assert quantile_from_buckets(0.95, {"10": 0, "+Inf": 4}) == 10.0  # beyond the top bucket


def series(svc, status="STATUS_CODE_UNSET", **extra):
    return tuple(sorted({"service_name": svc, "status_code": status, "span_kind": "SPAN_KIND_SERVER",
                         **extra}.items()))


def counters(at, calls, errors=None, buckets=None):
    """One Prometheus sample as raw series. calls / errors: {service: cumulative count};
    buckets: {service: {le: cumulative count}}."""
    c = {}
    for svc, n in calls.items():
        e = (errors or {}).get(svc, 0)
        c[series(svc)] = n - e
        if e:
            c[series(svc, "STATUS_CODE_ERROR")] = e
    b = {series(svc, le=le): v for svc, per in (buckets or {}).items() for le, v in per.items()}
    return {"t": at, "calls": c, "buckets": b}


class CounterVerifier(Verifier):
    """Real verify loop and sample() increases; counters, container, fast signal and serving
    evidence are scripted. samples: [(time a new Prometheus sample becomes visible, counters)]."""

    def __init__(self, samples, fast_ok_at=None, serving_ok=True, **kw):
        self.t = 0.0
        kw.setdefault("timeout_s", 300)
        super().__init__(consecutive=2, fast_interval_s=10, interval_s=30,
                         sleep=lambda s: setattr(self, "t", self.t + s), clock=lambda: self.t, **kw)
        self.samples, self.fast_ok_at, self.fast_calls = samples, fast_ok_at, 0
        self.serving_ok = serving_ok

    def counters(self, services):
        return [c for at, c in self.samples if at <= self.t][-1]

    def container(self, name):
        return {"ok": True, "status": "running", "health": "healthy"}

    def fast_signal(self, step, services, baseline, since):
        self.fast_calls += 1
        return {"ok": self.fast_ok_at is not None and self.t >= self.fast_ok_at, "services": {}}

    def serving(self, step, since):
        return {"ok": self.serving_ok, "target": self.root_of(step), "server_spans": int(self.serving_ok),
                "client_calls": 0, "example_trace": "t1" if self.serving_ok else None}


def test_sample_uses_counter_increases_and_waits_for_a_new_sample():
    v = CounterVerifier([
        (0, counters(0, {"checkout": 50}, {"checkout": 40},
                     {"checkout": {"100": 10, "10000": 50, "+Inf": 50}})),
        (60, counters(60, {"checkout": 80}, {"checkout": 40},
                      {"checkout": {"100": 38, "10000": 80, "+Inf": 80}})),
    ])
    assert v.sample(["checkout"]) == ({}, 0.0, False)          # reference sample
    assert v.sample(["checkout"])[2] is False                  # same sample: nothing new
    v.t = 60
    rows, dt, fresh = v.sample(["checkout"])
    assert fresh and dt == 60
    # only what happened since the reference: 30 calls, no new errors, mostly fast
    assert rows["checkout"]["calls"] == 30 and rows["checkout"]["error_rate"] == 0.0
    # bucket increases: 28 calls <= 100ms, 30 <= 10s; rank 28.5 -> 100 + 9900 * 0.5/2
    assert rows["checkout"]["rps"] == 0.5 and rows["checkout"]["p95_ms"] == 2575.0


def test_counter_reset_counts_from_zero_like_increase():
    assert increases({"a": 100.0, "b": 5.0}, {"a": 4.0, "b": 9.0, "c": 2.0}) == {"a": 4.0, "b": 4.0, "c": 2.0}
    # The collector restarted between samples: every series dropped. A plain difference would say
    # -96 calls; counting from zero gives the 4 calls since the reset, 3 of them errors.
    v = CounterVerifier([(0, counters(0, {"checkout": 100}, {"checkout": 40})),
                         (60, counters(60, {"checkout": 4}, {"checkout": 3}))])
    v.sample(["checkout"])
    v.t = 60
    rows, _, fresh = v.sample(["checkout"])
    assert fresh and rows["checkout"]["calls"] == 4 and rows["checkout"]["error_rate"] == 0.75
    assert Verifier.judge(rows["checkout"], BASE["checkout"], judge_rps=False)[0] is False  # not healthy


HEALTHY_SAMPLES = [
    (0, counters(0, {"checkout": 10, "payment": 5}, {"checkout": 8})),   # outage before the fix
    (30, counters(30, {"checkout": 15, "payment": 7}, {"checkout": 8})),
    (90, counters(90, {"checkout": 20, "payment": 9}, {"checkout": 8})),
]


def test_fast_path_reports_recovering_early_and_verifies_under_two_minutes(baseline):
    progress = []
    v = CounterVerifier(HEALTHY_SAMPLES, fast_ok_at=10)
    out = v.verify(STEP, ["payment", "checkout"], progress=progress.append)
    assert out["outcome"] == RECOVERED and out["seconds"] < 120
    assert out["recovering"]["at_s"] == 10 and registry.get(out["recovering"]["evidence"])["tool"] == "verify"
    assert len(out["checks"]) == 2                     # two metrics checks, each on a new sample
    assert any(p.startswith("[verify] payment: recovering after 10s") for p in progress)
    assert out["polls"] == 10                          # every 10 s instead of every 30 s


def test_fast_path_alone_never_declares_recovery(baseline):
    failing = [(0, counters(0, {"checkout": 10}, {"checkout": 0})),
               (60, counters(60, {"checkout": 20}, {"checkout": 10})),
               (120, counters(120, {"checkout": 30}, {"checkout": 20}))]
    v = CounterVerifier(failing, fast_ok_at=0, timeout_s=180)
    out = v.verify({**STEP, "action": "throttle_load_generator"}, ["checkout"])
    assert out["recovering"] is not None and out["outcome"] == NOT_RECOVERED


def test_fast_path_can_be_switched_off(baseline, monkeypatch):
    monkeypatch.setattr(config, "VERIFY_FAST_PATH", False)
    v = CounterVerifier(HEALTHY_SAMPLES, fast_ok_at=0)
    out = v.verify(STEP, ["payment", "checkout"])
    assert v.fast_calls == 0 and out["recovering"] is None and out["outcome"] == RECOVERED
    assert out["seconds"] == 90 and out["polls"] == 4  # 30 s polling without the fast path


def every_minute(until, calls, errors=None, extra=None):
    """New samples every 60 s with constant counters: time passes, nothing happens."""
    return [(t, counters(t, {**calls, **(extra or {})}, errors)) for t in range(0, until + 1, 60)]


def test_dead_low_traffic_service_with_zero_errors_is_not_recovered(baseline):
    # payment gets ~1 call a minute in the baseline. After the "fix" it gets none and nothing
    # errors: every check passes on paper (container running, no errors), but nobody saw it serve.
    v = CounterVerifier(every_minute(300, {"payment": 5}), serving_ok=False)
    out = v.verify(STEP, ["payment"])
    assert out["outcome"] == INCONCLUSIVE and out["reason"] == "no traffic observed"
    assert out["serving"] is None and len(out["checks"]) == 5 and all(c["ok"] for c in out["checks"])


def test_no_shop_traffic_is_inconclusive_for_every_service(monkeypatch):
    base = {**BASE, "frontend-proxy": {"rps": 10.0, "error_rate": 0.0}, "frontend": {"rps": 12.0}}
    config.BASELINE_FILE.write_text(json.dumps({"source": "mem", "services": base}))
    # the load stopped: samples keep arriving, entry counters do not move, nothing errors
    samples = every_minute(300, {"payment": 5, "checkout": 9}, extra={"frontend-proxy": 500, "frontend": 600})
    v = CounterVerifier(samples, serving_ok=True)
    out = v.verify(STEP, ["payment", "checkout"])
    assert out["outcome"] == INCONCLUSIVE and out["reason"] == "no traffic: shop traffic near zero"
    assert out["seconds"] == 120                       # two quiet checks in a row, then stop
    assert all(m["problems"] == ["no shop traffic"] and m["ok"] is None for m in out["services"].values())
    assert all(c["no_shop_traffic"] for c in out["checks"])


def test_serving_needs_a_successful_span_or_call_since_the_action(mem):
    since = mem.t

    def span(svc, sid, start_s, kind, parent=None, error=False, peer=None):
        tags = [{"key": "span.kind", "value": kind}] + ([{"key": "error", "value": True}] if error else [])
        if peer:
            tags.append({"key": "server.address", "value": peer})
        return {"spanID": sid, "operationName": "op", "processID": svc, "startTime": int(start_s * 1e6),
                "duration": 1000, "tags": tags,
                "references": [{"refType": "CHILD_OF", "spanID": parent}] if parent else []}

    def trace(*spans):
        return {"traceID": spans[0]["spanID"], "processes": {s["processID"]: {"serviceName": s["processID"]}
                                                           for s in spans}, "spans": list(spans)}

    v, step = Verifier(), {"action": "restart_service", "target": "valkey-cart", "step": 1}
    # a cache has no spans: a successful call to it from its caller counts
    mem.traces = [trace(span("cart", "c1", since + 2, "client", peer="valkey-cart:6379"))]
    s = v.serving(step, since)
    assert s["ok"] and s["client_calls"] == 1 and s["server_spans"] == 0
    mem.traces = [trace(span("cart", "c2", since + 2, "client", peer="valkey-cart:6379", error=True)),
                  trace(span("cart", "c3", since - 10, "client", peer="valkey-cart:6379"))]
    assert v.serving(step, since)["ok"] is False      # failed call, or one from before the action
    # a service with spans: its own successful server span counts
    mem.traces = [trace(span("payment", "p1", since + 1, "server"))]
    assert v.serving({**step, "target": "payment"}, since)["server_spans"] == 1


def test_fast_signal_reads_jaeger_spans_since_the_action(mem):
    since = mem.t
    mem.inspect = [{"Name": "/payment", "State": {"Running": True, "Status": "running",
                                                  "Health": {"Status": "healthy"}}}]

    def trace(start_s, error=False):
        tags = [{"key": "span.kind", "value": "server"}] + ([{"key": "error", "value": True}] if error else [])
        return {"traceID": f"t{start_s}", "processes": {"p1": {"serviceName": "payment"}},
                "spans": [{"spanID": f"s{start_s}", "operationName": "Charge", "processID": "p1",
                           "startTime": int(start_s * 1e6), "duration": 1000, "tags": tags}]}

    v, base = Verifier(), {"payment": {"rps": 0.02}, "valkey-cart": {"rps": 0.0}}
    mem.traces = [trace(since - 5, error=True)]           # failure from before the fix only
    assert v.fast_signal(STEP, ["payment"], base, since)["services"]["payment"]["state"] == "waiting"
    mem.traces = [trace(since + 2), trace(since - 5, error=True)]
    sig = v.fast_signal(STEP, ["payment", "valkey-cart"], base, since)
    assert sig["ok"] and sig["services"] == {"payment": {"state": "ok", "ok_spans": 1, "error_spans": 0}}
    mem.traces = [trace(since + 2), trace(since + 3, error=True)]
    assert v.fast_signal(STEP, ["payment"], base, since)["services"]["payment"]["state"] == "failing"
    mem.inspect[0]["State"]["Running"] = False
    mem.traces = [trace(since + 2)]
    assert v.fast_signal(STEP, ["payment"], base, since)["ok"] is False  # container not running


# --- verify-only mode --------------------------------------------------------------------------------

def test_verify_only_mode_after_a_manual_fix(monkeypatch):
    import requests
    from oncall import __main__ as cli
    from oncall import action_api, verify

    for cls in (action_api.HttpActionClient, action_api.DryRunClient, action_api.FakeClient):
        monkeypatch.setattr(cls, "call", lambda *a, **k: pytest.fail("Action API used in verify-only"))
    monkeypatch.setattr(requests, "request", lambda *a, **k: pytest.fail("HTTP used in verify-only"))

    diag = plan_for(("valkey-cart", "dependency_down"))
    record = {"run_id": "20261024-100000", "source": "live", "summary": diag,
              "evidence": registry.dump(), "steps": []}
    top_before = max(int(e["id"][1:]) for e in record["evidence"])
    config.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.RUNS_DIR / "20261024-100000.json"
    path.write_text(json.dumps(record))
    registry.reset()  # a new process: the registry starts empty

    class Recovered(StubVerifier):
        def __init__(self, **kw):
            super().__init__({"valkey-cart": RECOVERED})

        def verify(self, step, services, before=None, progress=None):
            out = super().verify(step, services, before, progress)
            out["evidence"] = [registry.add("verify", "metric", f"verify {step['target']} healthy")]
            return out

    monkeypatch.setattr(verify, "Verifier", Recovered)
    cli.cmd_verify(SimpleNamespace(run="20261024-100000", timeout=None))

    saved = json.loads(path.read_text())
    mv = saved["manual_verifications"][0]
    assert mv["outcome"] == "recovered" and mv["targets"][0]["involved"] == ["valkey-cart", "cart"]
    new_id = mv["targets"][0]["verify"]["evidence"][0]
    assert int(new_id[1:]) == top_before + 1          # evidence IDs continue the run's numbering
    assert {e["id"] for e in record["evidence"]} < {e["id"] for e in saved["evidence"]}
    event = logged()[-1]
    assert event["event"] == "manual_remediation" and event["target"] == "valkey-cart"
    assert event["outcome"] == "recovered" and "no Action API call" in event["note"]
    md = (config.RUNS_DIR / "20261024-100000.report.md").read_text()
    assert "## Manual remediation" in md and "no Action API call was made" in md
    assert "Manual remediation verified: recovered (valkey-cart)" in md
