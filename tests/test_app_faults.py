"""Application faults (containers healthy, the code failing) on offline fixtures, with a fake LLM:
the plan escalates instead of restarting a healthy container, a flood is throttled, and no tool output
carries feature-flag data. Scenario knowledge lives only in the fixtures and these tests."""
import json

import pytest

from oncall import config, sources
from oncall.action_api import FakeClient
from oncall.agent import run
from oncall.agent.actions import FIXED_BY
from oncall.agent.diagnosis import validate
from oncall.evidence import registry
from oncall.record import FLAG_TEXT
from oncall.sources.sanitize import contains_flags
from oncall.tools import _common
from oncall.tools.deploys import get_deploy_history
from oncall.tools.logs import search_logs
from oncall.tools.metrics import get_metric, get_service_health
from oncall.tools.traces import find_error_traces
from oncall.verify import RECOVERED
from tests.conftest import FIXTURES
from tests.schema_check import errors
from tests.test_agent_loop import block_tool, make_agent, response
from tests.test_remediate import StubVerifier, human

APP_FIXTURES = ["payment_app_error", "product_one_item", "homepage_flood", "two_app_faults",
                "unseen_cert_expired", "unseen_disk_full"]


@pytest.fixture
def use_fixture(monkeypatch, tmp_path):
    def use(name):
        monkeypatch.setattr(config, "BASELINE_FILE", tmp_path / "baseline.json")
        monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
        monkeypatch.setattr(config, "ACTION_LOG", tmp_path / "actions.jsonl")
        monkeypatch.setattr(config, "KILL_SWITCH_FILE", tmp_path / "STOP")
        _common._metric_names.clear()
        registry.reset()
        return sources.set_source("test", fixture_dir=FIXTURES / name, allow_test=True)
    yield use
    sources._source = None
    _common._metric_names.clear()


def no_flags(obj):
    text = json.dumps(obj, default=str)
    return not contains_flags(obj) and not FLAG_TEXT.search(text) and "flagd" not in text.lower()


@pytest.mark.parametrize("name", APP_FIXTURES)
def test_fixture_files_and_every_tool_output_are_flag_free(use_fixture, name):
    for path in (FIXTURES / name).rglob("*"):
        if path.is_file():
            assert "flag" not in path.read_text(encoding="utf-8").lower(), path
    use_fixture(name)
    outputs = [get_service_health(), get_deploy_history(), find_error_traces("frontend"),
               find_error_traces("frontend", errors_only=False), find_error_traces("checkout"),
               search_logs(["payment", "checkout", "product-catalog", "frontend"]),
               get_metric("rate(traces_span_metrics_calls_total[1m])")]
    assert all(no_flags(o) for o in outputs)
    assert no_flags(registry.dump())
    assert get_deploy_history()["changed_or_unhealthy"] == []  # every container healthy


def scripted(name, fault_class, component, proposed):
    """A fake LLM that calls the tools, then submits a diagnosis citing their real evidence IDs."""
    def script(n, kwargs):
        if n == 1:
            return response(block_tool("h", "get_service_health", {"window": "5m"}),
                            block_tool("d", "get_deploy_history", {"lookback": "30m"}),
                            block_tool("t", "find_error_traces", {"service": "frontend", "errors_only": name != "homepage_flood"}),
                            block_tool("l", "search_logs", {"services": [component]}))
        results = {r["tool_use_id"]: json.loads(r["content"]) for r in kwargs["messages"][-1]["content"]}
        ev = [results["h"]["flagged"][0]["evidence"], results["d"]["evidence"]]
        ev += [o["evidence"] for o in results["t"].get("origins", [])][:1]
        ev += [p["evidence"] for p in results["l"].get("patterns", []) if p.get("evidence")][:1]
        return response(block_tool("s", "submit_diagnosis", {
            "headline": f"{component} {fault_class}", "affected_services": [], "anomalies": [],
            "root_causes": [{"component": component, "fault_class": fault_class, "status": "active",
                             "cause": f"{component} application code fails; its container is running and healthy",
                             "confidence": 0.85, "evidence": ev}],
            "proposed_action": proposed, "prevention": [], "open_questions": []}))
    return script


def agent_for(monkeypatch, script, client):
    agent, _ = make_agent(monkeypatch, script)
    agent.action_client, agent.approve = client, human({})
    agent.verifier = StubVerifier({"load-generator": RECOVERED})
    return agent


@pytest.mark.parametrize("name,fault_class,component", [
    ("payment_app_error", "service_error", "payment"),
    ("product_one_item", "input_specific_error", "product-catalog"),
])
def test_healthy_container_with_application_errors_escalates(use_fixture, monkeypatch, name, fault_class, component):
    use_fixture(name)
    client = FakeClient()
    # the model even asks to restart the healthy container; the fault class decides, so it escalates
    proposed = {"action": "restart_service", "target": component, "reason": "restart it"}
    rec = agent_for(monkeypatch, scripted(name, fault_class, component, proposed), client).run(
        json.loads((FIXTURES / name / "alert.json").read_text()))
    plan = rec["remediation_plan"]
    assert plan["steps"] == [] and client.calls == []
    esc = rec["escalations"][0]
    assert (esc["target"], esc["fault_class"]) == (component, fault_class) and esc["next_steps"]
    assert rec["outcome"]["status"] == "escalated" and errors(rec) == []
    assert any("replaced by escalate_to_human" in n for n in rec["diagnosis"]["validation"]["notes"])
    assert no_flags({k: v for k, v in rec.items() if k != "path"})


def test_flood_produces_a_throttle_step(use_fixture, monkeypatch):
    use_fixture("homepage_flood")
    client = FakeClient()
    proposed = {"action": "throttle_load_generator", "target": "load-generator", "reason": "entry traffic 5x baseline"}
    rec = agent_for(monkeypatch, scripted("homepage_flood", "traffic_flood", "frontend-proxy", proposed), client).run(
        json.loads((FIXTURES / "homepage_flood" / "alert.json").read_text()))
    steps = rec["remediation_plan"]["steps"]
    assert [(s["action"], s["target"]) for s in steps] == [("throttle_load_generator", "load-generator")]
    assert steps[0]["rollback"]["note"] == "remove the throttle"
    assert client.calls[0][:2] == ("throttle_load_generator", "load-generator")
    assert rec["outcome"]["status"] == "recovered" and errors(rec) == []


@pytest.mark.parametrize("fault_class", ["service_error", "input_specific_error", "memory_leak",
                                         "cpu_or_gc_pressure", "traffic_flood"])
def test_no_application_fault_class_restarts_anything(mem, fault_class):
    registry.reset()
    eid = registry.add("t", "trace", "application errors; container running and healthy")
    d = validate({"headline": "h", "affected_services": [], "anomalies": [], "open_questions": [],
                  "root_causes": [{"component": "payment", "fault_class": fault_class, "cause": "code fails",
                                   "confidence": 0.9, "evidence": [eid]}],
                  "proposed_action": {"action": "restart_service", "target": "payment", "reason": "restart"}})
    actions = [s["action"] for s in d["remediation_plan"]["steps"]]
    assert "restart_service" not in actions
    expected = ["throttle_load_generator"] if fault_class == "traffic_flood" else []
    assert actions == expected and FIXED_BY[fault_class] in ("toggle_feature_flag", "throttle_load_generator")
