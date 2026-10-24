"""Risk at the approval prompt (based on Shivakumar's Day 1 approval gate)."""
import json
import sys

import pytest

from oncall.action_api import FakeClient
from oncall.remediate import make_approver
from tests.test_incident_record import diagnosis_script, fake_run
from tests.test_remediate import plan_for


@pytest.fixture
def terminal(monkeypatch):
    """Pretend a human sits at a terminal and answers y."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")


def step_with(risk=None, mem=None):
    diag = plan_for(("payment", "container_stopped_or_restarting"))
    step = diag["remediation_plan"]["steps"][0]
    step["expected_outcome"] = "checkout errors stop"
    if risk is not None:
        step["risk_assessment"] = risk
    return diag, step


def test_risk_is_shown_between_expected_and_rollback(mem, terminal, capsys):
    diag, step = step_with("Low: payment is already down")
    approval = make_approver()(step, diag)
    lines = capsys.readouterr().out.splitlines()
    expected = next(i for i, l in enumerate(lines) if l.startswith("Expected"))
    assert lines[expected + 1] == "Risk       : Low: payment is already down"
    assert lines[expected + 2].startswith("Rollback")
    assert approval["status"] == "approved" and approval["risk"] == "Low: payment is already down"


@pytest.mark.parametrize("risk", [None, "", "   "])
def test_missing_risk_is_not_assessed(mem, terminal, capsys, risk):
    diag, step = step_with(risk)
    approval = make_approver()(step, diag)
    assert "Risk       : not assessed" in capsys.readouterr().out
    assert approval["risk"] == "not assessed"


def test_every_approval_record_keeps_the_risk(mem):
    diag, step = step_with("Medium: carts may be emptied")
    assert make_approver(auto_approve=True)(step, diag)["risk"] == "Medium: carts may be emptied"
    assert make_approver(interactive=False)(step, diag)["risk"] == "Medium: carts may be emptied"


def with_risk(n, kwargs):
    response = diagnosis_script(n, kwargs)
    for block in response.content:
        if block.type == "tool_use" and block.name == "submit_diagnosis":
            block.input["proposed_action"]["risk_assessment"] = "Low: the container is already down"
    return response


def test_risk_is_saved_in_the_incident_record_and_report(payment_down, monkeypatch, terminal):
    rec = fake_run(monkeypatch, script=with_risk, approve=make_approver())
    data = json.loads(open(rec["path"], encoding="utf-8").read())
    approval = data["remediation_plan"]["steps"][0]["approval"]
    assert approval["approved_by"] == "human" and approval["risk"] == "Low: the container is already down"
    report = open(rec["report_paths"]["report"], encoding="utf-8").read()
    assert "- Risk (as shown to the approver): Low: the container is already down" in report


def test_unassessed_risk_in_record_and_report(payment_down, monkeypatch):
    rec = fake_run(monkeypatch, approve=make_approver(auto_approve=True), client=FakeClient(live=False))
    data = json.loads(open(rec["path"], encoding="utf-8").read())
    assert data["remediation_plan"]["steps"][0]["approval"]["risk"] == "not assessed"
    assert "- Risk (as shown to the approver): not assessed" in open(rec["report_paths"]["report"]).read()
