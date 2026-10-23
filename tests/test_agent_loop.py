"""Agent loop mechanics with a scripted fake LLM (no API key or network needed)."""
import copy
import json
from types import SimpleNamespace

from oncall.agent import loop
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


def make_agent(monkeypatch, script):
    client = ScriptedClient(script)
    monkeypatch.setattr(loop.anthropic, "Anthropic", lambda: client)
    agent = loop.Agent(verbose=False)
    agent.use_fallbacks = False
    return agent, client


def test_loop_runs_tools_then_submits(fake, monkeypatch):
    fake.traces = [propagated_payment_failure(f"t{i}") for i in range(3)]

    def script(n, kwargs):
        if n == 1:
            return response(block_tool("u1", "find_error_traces", {"service": "frontend"}))
        # The model cites the evidence ID the tool returned, plus one invented ID.
        result = json.loads(kwargs["messages"][-1]["content"][0]["content"])
        eid = result["origins"][0]["evidence"]
        return response(block_tool("u2", "submit_diagnosis", {
            "status": "diagnosed", "summary": "payment fails", "symptoms": [],
            "root_causes": [{"component": "payment", "fault_class": "service_error",
                             "cause": "charge fails", "confidence": 0.85, "evidence": [eid, "E77"]}],
            "affected_services": ["frontend", "checkout", "payment"], "confidence": 0.85,
            "proposed_action": {"action": "none", "target": "", "reason": ""},
            "escalation_reason": "", "next_checks": []}))

    agent, client = make_agent(monkeypatch, script)
    rec = agent.run({"service": "frontend", "signal": "error_rate", "value": 0.3})
    assert rec["outcome"] == "submitted"
    d = rec["diagnosis"]
    assert d["status"] == "diagnosed" and d["root_causes"][0]["component"] == "payment"
    assert d["validation"]["dropped_evidence_ids"] == ["E77"]
    assert rec["steps"][0]["tool"] == "find_error_traces"
    saved = json.loads(open(rec["path"], encoding="utf-8").read())
    assert saved["evidence"] and saved["output"]["run_id"] == rec["run_id"]
    assert "thinking" in client.calls[0] and client.calls[0]["model"] == "claude-opus-5-5"


def test_step_budget_forces_escalation(fake, monkeypatch):
    monkeypatch.setattr(loop.config, "MAX_STEPS", 3)
    agent, client = make_agent(monkeypatch, lambda n, kw: response(
        block_tool(f"u{n}", "list_trace_services", {})))
    rec = agent.run({"service": "cart"})
    assert rec["outcome"] == "incomplete" and rec["diagnosis"]["status"] == "escalate"
    assert len(client.calls) == 3
    last = client.calls[-1]["messages"][-1]
    assert last["role"] == "user" and last["content"][0]["type"] == "tool_result"
    assert "Step budget exhausted" in last["content"][-1]["text"]


def test_kill_switch_stops_run(fake, monkeypatch):
    loop.config.KILL_SWITCH_FILE.write_text("stop")
    agent, client = make_agent(monkeypatch, lambda n, kw: response())
    rec = agent.run({"service": "cart"})
    assert rec["outcome"] == "killed" and not client.calls
