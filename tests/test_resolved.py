"""Root causes resolved before any action get no step; every step's reason comes from its own root
cause (live run 20261024-094121 restarted an already-recovered payment with a boilerplate reason)."""
import json

import pytest

from oncall.action_api import FakeClient
from oncall.agent.diagnosis import validate
from oncall.evidence import registry
from oncall.remediate import run_plan
from oncall.report import build_report
from tests.schema_check import errors
from tests.test_remediate import StubVerifier, human


@pytest.fixture(autouse=True)
def clean(mem):
    registry.reset()


def rc(component, fault_class, status=None, cause=None, confidence=0.9):
    eid = registry.add("t", "container", f"{component} observation")
    out = {"component": component, "fault_class": fault_class, "confidence": confidence, "evidence": [eid],
           "cause": cause if cause is not None else f"{component} container exited (exit 137)"}
    if status is not None:
        out["status"] = status
    return out


def diagnose(*root_causes, proposed=None):
    return validate({"headline": "h", "affected_services": [], "anomalies": [], "open_questions": [],
                     "root_causes": list(root_causes),
                     "proposed_action": proposed or {"action": "restart_service", "target": "valkey-cart",
                                                     "reason": "valkey-cart is stopped; start it"}})


def test_resolved_root_cause_gets_no_step():
    d = diagnose(rc("payment", "container_stopped_or_restarting", "resolved",
                    cause="payment exited at 04:05 but is running and serving again since 04:09"),
                 proposed={"action": "restart_service", "target": "payment", "reason": "restart it"})
    plan = d["remediation_plan"]
    assert plan["steps"] == [] and plan["escalations"] == []
    assert plan["resolved"][0]["component"] == "payment" and plan["resolved"][0]["note"] == "resolved before action"
    assert d["proposed_action"]["action"] == "none"
    assert any("all root causes resolved" in n for n in d["validation"]["notes"])


def test_mixed_only_the_active_root_cause_gets_a_step():
    # run 20261024-094121: valkey-cart still down, payment already back
    d = diagnose(rc("valkey-cart", "dependency_down", "active", cause="valkey-cart exited; cart cannot connect"),
                 rc("payment", "container_stopped_or_restarting", "resolved",
                    cause="payment restarted at 04:09 and serves again"))
    steps = d["remediation_plan"]["steps"]
    assert [(s["action"], s["target"]) for s in steps] == [("restart_service", "valkey-cart")]
    assert [r["component"] for r in d["remediation_plan"]["resolved"]] == ["payment"]


def test_steps_never_get_a_boilerplate_reason():
    d = diagnose(rc("valkey-cart", "dependency_down", cause="valkey-cart exited; cart cannot connect"),
                 rc("payment", "container_stopped_or_restarting", cause="payment exited with code 137"))
    reasons = {s["target"]: s["reason"] for s in d["remediation_plan"]["steps"]}
    assert reasons["valkey-cart"] == "valkey-cart is stopped; start it"            # the model's own words
    assert "payment exited with code 137" in reasons["payment"]                     # its own root cause
    assert not any("is fixed by" in r for r in reasons.values())                     # the old boilerplate


def test_status_defaults_to_active_and_bad_values_are_noted():
    d = diagnose(rc("payment", "container_stopped_or_restarting"))
    assert d["root_causes"][0]["status"] == "active" and d["remediation_plan"]["steps"]
    d = diagnose(rc("payment", "container_stopped_or_restarting", status="fixed?"))
    assert d["root_causes"][0]["status"] == "active"
    assert any("treated as active" in n for n in d["validation"]["notes"])


def test_resolved_appears_in_the_report_and_record_outcome(payment_down, monkeypatch):
    from tests.test_agent_loop import block_tool, response
    from tests.test_incident_record import fake_run

    def script(n, kwargs):
        if n == 1:
            return response(block_tool("d", "get_deploy_history", {"lookback": "30m"}))
        eid = json.loads(kwargs["messages"][-1]["content"][0]["content"])["changed_or_unhealthy"][0]["evidence"]
        return response(block_tool("s", "submit_diagnosis", {
            "headline": "payment had stopped but is back", "affected_services": [], "anomalies": [],
            "root_causes": [{"component": "payment", "fault_class": "container_stopped_or_restarting",
                             "status": "resolved", "cause": "payment is running and serving again",
                             "confidence": 0.8, "evidence": [eid]}],
            "proposed_action": {"action": "restart_service", "target": "payment", "reason": "restart"},
            "prevention": [], "open_questions": []}))

    client = FakeClient()
    rec = fake_run(monkeypatch, script=script, client=client)
    data = json.loads(open(rec["path"], encoding="utf-8").read())
    assert errors(data) == [] and client.calls == []  # nothing executed
    assert data["outcome"]["status"] == "escalated" and "resolved before action" in data["outcome"]["summary"]
    report = open(rec["report_paths"]["report"], encoding="utf-8").read()
    assert "(resolved before action)" in report and "Resolved before action (no step created):" in report


def test_run_plan_with_only_resolved_does_nothing(mem):
    d = diagnose(rc("payment", "container_stopped_or_restarting", "resolved"))
    client = FakeClient()
    plan = run_plan(d, client, human({}), StubVerifier({}), log=lambda s: None)
    assert client.calls == [] and plan["outcome"] == "escalated"
    md = build_report({"run_id": "r", "summary": d, "evidence": registry.dump(), "steps": []})
    assert "Resolved before action" in md
