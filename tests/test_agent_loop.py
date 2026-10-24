"""Agent run mechanics with a scripted fake LLM (no API key or network needed).

The scripted model plays the LLM's part; everything else (tools, fixtures, registry, validator,
run record) is the real code path.
"""
import copy
import json
from types import SimpleNamespace

from oncall.agent import run
from tests.conftest import FIXTURES
from tests.test_tools import propagated_payment_failure


def block_tool(tid, name, inp):
    return SimpleNamespace(type="tool_use", id=tid, name=name, input=inp)


def response(*blocks, stop="tool_use"):
    return SimpleNamespace(content=list(blocks), stop_reason=stop,
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5))


class ScriptedClient:
    def __init__(self, script):
        self.script = script
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        kwargs = {**kwargs, "messages": copy.deepcopy(kwargs["messages"])}  # snapshot per request
        self.calls.append(kwargs)
        return self.script(len(self.calls), kwargs)


def make_agent(monkeypatch, script, **kw):
    client = ScriptedClient(script)
    monkeypatch.setattr(run.anthropic, "Anthropic", lambda: client)
    agent = run.Agent(verbose=True, **kw)
    agent.use_fallbacks = False
    return agent, client


def tool_results(kwargs):
    """Tool results the agent sent back in its latest request, by tool_use_id."""
    return {r["tool_use_id"]: json.loads(r["content"])
            for r in kwargs["messages"][-1]["content"] if r.get("type") == "tool_result"}


def submit(tid, component, evidence, anomalies=(), affected=(), confidence=0.85):
    return block_tool(tid, "submit_diagnosis", {
        "headline": "h", "affected_services": list(affected), "anomalies": list(anomalies),
        "root_causes": [{"component": component, "fault_class": "container_stopped_or_restarting",
                         "cause": "r", "confidence": confidence, "evidence": evidence}],
        "proposed_action": {"action": "restart_service", "target": component, "reason": "r"},
        "open_questions": []})


def test_end_to_end_test_mode_checkout_errors_caused_by_payment(payment_down, monkeypatch, capsys):
    """Alert on checkout errors; the run must end at payment with real evidence IDs."""
    alert = json.loads((FIXTURES / "payment_down" / "alert.json").read_text())
    seen = {}

    def script(n, kwargs):
        if n == 1:
            return response(block_tool("h", "get_service_health", {"window": "5m"}),
                            block_tool("d", "get_deploy_history", {"lookback": "30m"}))
        seen.update(tool_results(kwargs))
        if n == 2:
            return response(block_tool("t", "find_error_traces", {"service": "checkout"}),
                            block_tool("l", "search_logs", {"services": ["checkout"], "since": "5m"}))
        checkout = seen["h"]["flagged"][0]
        payment_silent = next(r for r in seen["h"]["flagged"] if r["service"] == "payment")
        payment_container = seen["d"]["changed_or_unhealthy"][0]
        trace_origin = seen["t"]["origins"][0]
        log_pattern = seen["l"]["patterns"][0]
        return response(submit(
            "s", "payment",
            [payment_container["evidence"], trace_origin["evidence"], payment_silent["evidence"], "E999"],
            anomalies=[
                {"service": "checkout", "signal": "error_rate", "observed": "95.2%",
                 "baseline": "0.0%", "evidence": [checkout["evidence"]]},
                {"service": "checkout", "signal": "log_errors", "observed": "6x name resolver error",
                 "baseline": "unknown", "evidence": [log_pattern["evidence"]]},
                {"service": "cart", "signal": "made_up", "observed": "x", "baseline": "y",
                 "evidence": ["E555"]},
            ],
            affected=[{"service": "checkout", "impact": "orders fail", "evidence": [checkout["evidence"]]}]))

    agent, _ = make_agent(monkeypatch, script)
    rec = agent.run(alert)

    assert rec["investigation"]["outcome"] == "submitted" and rec["metadata"]["mode"] == "test"
    assert rec["metadata"]["source_detail"] == "fixtures tests/fixtures/payment_down"
    assert rec["path"].endswith("-test.json")
    assert [s["tool"] for s in rec["investigation"]["steps"]] == ["get_service_health", "get_deploy_history",
                                                 "find_error_traces", "search_logs"]
    s = rec["diagnosis"]
    assert s["status"] == "diagnosed"
    rc = s["root_causes"][0]
    assert rc["component"] == "payment" and rc["fault_class"] == "container_stopped_or_restarting"
    assert "E999" not in rc["evidence"] and len(rc["evidence"]) == 3
    assert any("exited" in t for t in rc["evidence_detail"].values())
    assert s["proposed_action"] == {**s["proposed_action"], "action": "restart_service", "dry_run": True}
    assert [a["signal"] for a in s["anomalies"]] == ["error_rate", "log_errors"]  # made-up one dropped
    assert s["validation"]["dropped_evidence_ids"] == ["E555", "E999"]
    saved = json.loads(open(rec["path"], encoding="utf-8").read())
    assert saved["diagnosis"] == s and len(saved["evidence"]) >= 5
    assert saved["metadata"]["mode"] == "test" and saved["status"] == "complete"

    out = capsys.readouterr().out
    assert "[1] -> get_service_health(window='5m')" in out          # live per-call line
    assert "DIAGNOSIS [diagnosed] source=test" in out
    assert out.count("TEST MODE") == 2  # banner at start and in the summary
    assert "dropped anomaly without valid evidence: cart" in out


def test_root_cause_without_valid_evidence_escalates(payment_down, monkeypatch):
    agent, _ = make_agent(monkeypatch, lambda n, kw: response(submit("s", "payment", ["E1"])))
    rec = agent.run({"service": "checkout"})  # no tool ran, so E1 does not exist
    s = rec["diagnosis"]
    assert s["root_causes"] == [] and s["suspected_origin"]["service"] == "unknown"
    assert s["status"] == "escalate" and s["proposed_action"]["action"] == "escalate_to_human"


def test_low_confidence_escalates(mem, monkeypatch):
    mem.traces = [propagated_payment_failure("t1")]

    def script(n, kwargs):
        if n == 1:
            return response(block_tool("u1", "find_error_traces", {"service": "frontend"}))
        eid = tool_results(kwargs)["u1"]["origins"][0]["evidence"]
        return response(submit("u2", "payment", [eid], confidence=0.4))

    agent, client = make_agent(monkeypatch, script)
    rec = agent.run({"service": "frontend"})
    assert rec["diagnosis"]["status"] == "escalate"
    assert "thinking" in client.calls[0] and client.calls[0]["model"] == "claude-opus-5-5"


def test_default_step_cap_is_10(mem, monkeypatch):
    agent, client = make_agent(monkeypatch, lambda n, kw: response(
        block_tool(f"u{n}", "list_trace_services", {})))
    rec = agent.run({"service": "cart"})
    assert len(client.calls) == 10
    assert rec["investigation"]["outcome"] == "incomplete"
    assert rec["diagnosis"]["suspected_origin"]["service"] == "unknown"
    last = client.calls[-1]["messages"][-1]
    assert last["role"] == "user" and last["content"][0]["type"] == "tool_result"
    assert "Step budget exhausted" in last["content"][-1]["text"]


def test_kill_switch_stops_run(mem, monkeypatch):
    run.config.KILL_SWITCH_FILE.write_text("stop")
    agent, client = make_agent(monkeypatch, lambda n, kw: response())
    rec = agent.run({"service": "cart"})
    assert rec["investigation"]["outcome"] == "killed" and not client.calls


def test_checkpoint2_diagnosis_with_root_cause_and_approval(mem, monkeypatch):
    mem.traces = [propagated_payment_failure("t1")]

    def script(n, kwargs):
        if n == 1:
            return response(block_tool("u1", "find_error_traces", {"service": "frontend"}))
        eid = tool_results(kwargs)["u1"]["origins"][0]["evidence"]
        return response(block_tool("u2", "submit_diagnosis", {
            "headline": "Payment container stopped causing checkout failures",
            "affected_services": [{"service": "checkout", "impact": "checkout errors", "evidence": [eid]}],
            "anomalies": [{"service": "checkout", "signal": "error_rate", "observed": "100%", "baseline": "0%", "evidence": [eid]}],
            "root_causes": [{
                "component": "payment",
                "fault_class": "container_stopped_or_restarting",
                "cause": "Payment container is stopped; gRPC connection refused",
                "confidence": 0.92,
                "evidence": [eid],
            }],
            "proposed_action": {
                "action": "restart_service",
                "target": "payment",
                "reason": "Restart container to resume charge endpoint",
                "risk_assessment": "Low risk; service is already down",
                "expected_outcome": "Checkout error rate returns to 0%",
                "dry_run": True,
            },
            "open_questions": ["Check if restart loop occurs after start"],
        }))

    agent, _ = make_agent(monkeypatch, script, auto_approve=True)
    rec = agent.run({"service": "frontend"})
    assert rec["investigation"]["outcome"] == "submitted"
    s = {**rec["diagnosis"], "remediation_plan": rec["remediation_plan"]}
    assert s["status"] == "diagnosed"
    assert s["confidence"] == 0.92
    assert s["root_causes"][0]["component"] == "payment"
    assert s["root_causes"][0]["fault_class"] == "container_stopped_or_restarting"
    assert len(s["root_causes"][0]["evidence"]) == 1 and s["root_causes"][0]["evidence"][0] in s["root_causes"][0]["evidence_detail"]
    assert s["proposed_action"]["action"] == "restart_service"
    step = s["remediation_plan"]["steps"][0]
    assert step["approval"]["status"] == "approved" and step["approval"]["approved_by"] == "auto"
    assert step["execution"]["status"] == "dry_run" and step["dry_run"] is True  # auto = dry run only
    assert s["remediation_plan"]["outcome"] == "dry_run"


def test_checkpoint2_diagnosis_escalation_when_confidence_low(mem, monkeypatch):
    mem.traces = [propagated_payment_failure("t1")]

    def script(n, kwargs):
        if n == 1:
            return response(block_tool("u1", "find_error_traces", {"service": "frontend"}))
        eid = tool_results(kwargs)["u1"]["origins"][0]["evidence"]
        return response(block_tool("u2", "submit_diagnosis", {
            "headline": "Uncertain anomaly",
            "affected_services": [{"service": "checkout", "impact": "possible errors", "evidence": [eid]}],
            "anomalies": [],
            "root_causes": [{
                "component": "payment",
                "fault_class": "unknown",
                "cause": "Weak signal on payment",
                "confidence": 0.45,
                "evidence": [eid],
            }],
            "proposed_action": {"action": "restart_service", "target": "payment", "reason": "Guessing"},
            "open_questions": [],
        }))

    agent, _ = make_agent(monkeypatch, script, auto_approve=False)
    rec = agent.run({"service": "frontend"})
    s = {**rec["diagnosis"], "remediation_plan": rec["remediation_plan"]}
    assert s["status"] == "escalate"
    assert s["proposed_action"]["action"] == "escalate_to_human"
    assert s["remediation_plan"]["steps"] == [] and s["remediation_plan"]["outcome"] == "escalated"


def test_checkpoint2_invented_evidence_in_root_cause_dropped(mem, monkeypatch):
    def script(n, kwargs):
        return response(block_tool("u1", "submit_diagnosis", {
            "headline": "Invented root cause",
            "affected_services": [],
            "anomalies": [],
            "root_causes": [{
                "component": "cart",
                "fault_class": "memory_leak",
                "cause": "Invented claim without tools",
                "confidence": 0.99,
                "evidence": ["E999"],
            }],
            "proposed_action": {"action": "restart_service", "target": "cart", "reason": "No evidence"},
            "open_questions": [],
        }))

    agent, _ = make_agent(monkeypatch, script)
    rec = agent.run({"service": "cart"})
    s = rec["diagnosis"]
    assert s["status"] == "escalate"
    assert len(s["root_causes"]) == 0
    assert "E999" in s["validation"]["dropped_evidence_ids"]
