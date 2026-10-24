"""Optional reviewer agent (stretch goal 2), with a fake LLM: it can only lower confidence, never add
or change steps, its failures never break a run, and it costs nothing when switched off."""
import json
import sys
import time

import pytest

from oncall import config
from oncall.action_api import FakeClient
from oncall.agent.reviewer import REVIEW_TOOL, reviewer_line
from oncall.remediate import make_approver
from tests.schema_check import errors
from tests.test_agent_loop import block_tool, make_agent, response
from tests.test_incident_record import ALERT, diagnosis_script
from tests.test_remediate import StubVerifier, human
from oncall.verify import RECOVERED


def review_script(review=None, fail=None, sleep=0):
    """Turns 1-2: the normal investigation (payment stopped). Turn 3: the reviewer's call."""
    def script(n, kwargs):
        if n <= 2:
            return diagnosis_script(n, kwargs)
        assert [t["name"] for t in kwargs["tools"]] == ["submit_review"]  # no investigation tools
        assert "REGISTERED EVIDENCE" in kwargs["messages"][0]["content"]
        if sleep:
            time.sleep(sleep)
        if fail:
            raise fail
        return response(block_tool("r", "submit_review", review))
    return script


def run(monkeypatch, script, reviewer=True):
    monkeypatch.setattr(config, "REVIEWER", reviewer)
    agent, client = make_agent(monkeypatch, script)
    agent.action_client, agent.approve = FakeClient(live=False), human({})
    agent.verifier = StubVerifier({"payment": RECOVERED})
    rec = agent.run(dict(ALERT))
    assert errors({k: v for k, v in rec.items() if k != "path"}) == []
    return rec, client


def baseline_plan(monkeypatch):
    rec, _ = run(monkeypatch, review_script(), reviewer=False)
    return [(s["action"], s["target"]) for s in rec["remediation_plan"]["steps"]]


def test_agree_leaves_confidence_and_plan_unchanged(payment_down, monkeypatch):
    rec, client = run(monkeypatch, review_script({"verdict": "agree", "objections": [], "confidence_adjustment": 0}))
    review = rec["review"]
    assert (review["status"], review["verdict"], review["confidence_before"], review["confidence_after"]) == \
        ("done", "agree", 0.9, 0.9)
    assert rec["diagnosis"]["confidence"] == 0.9 and rec["diagnosis"]["status"] == "diagnosed"
    assert [(s["action"], s["target"]) for s in rec["remediation_plan"]["steps"]] == [("restart_service", "payment")]
    assert len(client.calls) == 3
    acc = rec["accounting"]  # the review's tokens are counted, but not as an investigation turn
    assert acc["reviewer_tokens"] == {"in": 10, "out": 5} and acc["tokens_in"] == 30 and acc["steps_used"] == 2


def test_disagree_lowers_confidence_and_the_alternative_is_never_acted_on(payment_down, monkeypatch):
    expected_plan = baseline_plan(monkeypatch)
    rec, _ = run(monkeypatch, review_script({
        "verdict": "disagree", "confidence_adjustment": -0.3,
        "objections": [{"text": "payment exited cleanly; something stopped it on purpose", "evidence": ["E1"]}],
        "alternative_root_cause": {"component": "cart", "fault_class": "dependency_down", "reason": "manual stop",
                                   "evidence": ["E1"]}}))
    d, review = rec["diagnosis"], rec["review"]
    assert (review["confidence_before"], review["confidence_after"]) == (0.9, 0.6)  # at, not below, 0.6
    assert d["confidence"] == 0.6 and d["root_causes"][0]["confidence"] == 0.6 and d["status"] == "diagnosed"
    assert review["alternative_root_cause"]["note"] == "shown to the human only; never acted on"
    steps = [(s["action"], s["target"]) for s in rec["remediation_plan"]["steps"]]
    assert steps == expected_plan and ("restart_service", "cart") not in steps


def test_disagree_below_threshold_removes_the_step_and_escalates_with_objections(payment_down, monkeypatch):
    def lower_first(n, kwargs):
        out = diagnosis_script(n, kwargs)
        if n == 2:  # a diagnosis at 0.8: the reviewer's -0.3 takes it below 0.6
            for b in out.content:
                if b.type == "tool_use":
                    b.input["root_causes"][0]["confidence"] = 0.8
        return out

    def script(n, kwargs):
        if n <= 2:
            return lower_first(n, kwargs)
        return response(block_tool("r", "submit_review", {
            "verdict": "disagree", "confidence_adjustment": -0.3,
            "objections": [{"text": "checkout errors started before payment stopped", "evidence": ["E1"]}]}))

    rec, _ = run(monkeypatch, script)
    assert rec["review"]["confidence_after"] == 0.5 and rec["review"]["escalated"] is True
    assert rec["diagnosis"]["status"] == "escalate" and rec["remediation_plan"]["steps"] == []
    esc = rec["escalations"][0]
    assert "reviewer lowered confidence to 0.5" in esc["reason"]
    assert "Reviewer objection: checkout errors started before payment stopped" in esc["next_steps"]
    assert rec["outcome"]["status"] == "escalated"


@pytest.mark.parametrize("adjustment", [0.5, 0.3, "high", None])
def test_reviewer_can_never_raise_confidence_or_change_the_plan(payment_down, monkeypatch, adjustment):
    expected_plan = baseline_plan(monkeypatch)
    rec, _ = run(monkeypatch, review_script({
        "verdict": "agree", "confidence_adjustment": adjustment, "objections": [],
        # attempts to steer the plan are ignored: there is no such field in the review contract
        "proposed_action": {"action": "restart_service", "target": "checkout"}, "approve": True,
        "steps": [{"action": "restart_service", "target": "frontend"}]}))
    assert rec["review"]["adjustment"] == 0.0 and rec["review"]["confidence_after"] == 0.9
    assert [(s["action"], s["target"]) for s in rec["remediation_plan"]["steps"]] == expected_plan
    assert all(s["approval"]["status"] == "approved" and s["approval"]["approved_by"] == "human"
               for s in rec["remediation_plan"]["steps"])  # approval still comes only from the approver


def test_adjustment_is_clamped_at_minus_0_3(payment_down, monkeypatch):
    rec, _ = run(monkeypatch, review_script({"verdict": "disagree", "confidence_adjustment": -0.9,
                                             "objections": []}))
    assert rec["review"]["adjustment"] == -0.3 and rec["review"]["confidence_after"] == 0.6


@pytest.mark.parametrize("fail,sleep,reason", [
    (RuntimeError("overloaded"), 0, "review skipped: RuntimeError: overloaded"),
    (None, 1.0, "review skipped: timeout after 0.2s"),
])
def test_reviewer_error_or_timeout_is_skipped_and_the_run_completes(payment_down, monkeypatch, fail, sleep, reason):
    monkeypatch.setattr(config, "REVIEWER_TIMEOUT_S", 0.2)
    expected_plan = baseline_plan(monkeypatch)
    rec, _ = run(monkeypatch, review_script({"verdict": "agree", "objections": [], "confidence_adjustment": 0},
                                            fail=fail, sleep=sleep))
    assert rec["review"]["status"] == "skipped" and rec["review"]["reason"] == reason
    assert rec["diagnosis"]["confidence"] == 0.9 and rec["status"] == "complete"
    assert [(s["action"], s["target"]) for s in rec["remediation_plan"]["steps"]] == expected_plan


def test_objections_citing_unknown_evidence_are_dropped(payment_down, monkeypatch):
    rec, _ = run(monkeypatch, review_script({
        "verdict": "doubt", "confidence_adjustment": -0.1,
        "objections": [{"text": "made up", "evidence": ["E999"]}, {"text": "no ids", "evidence": []},
                       {"text": "real concern", "evidence": ["E1", "E888"]}]}))
    review = rec["review"]
    assert review["objections"] == [{"text": "real concern", "evidence": ["E1"]}]
    assert review["dropped_objections"] == 2 and review["confidence_after"] == 0.8
    assert rec["remediation_plan"]["steps"]  # 0.8 stays above the threshold: plan kept


def test_reviewer_off_makes_no_call(payment_down, monkeypatch):
    rec, client = run(monkeypatch, review_script(), reviewer=False)
    assert len(client.calls) == 2 and rec["review"] is None
    assert rec["accounting"]["reviewer_tokens"] == {"in": 0, "out": 0}


def test_kill_switch_is_checked_before_the_review(payment_down, monkeypatch):
    def script(n, kwargs):
        if n == 2:
            config.KILL_SWITCH_FILE.write_text("stop")  # switched on while the model was diagnosing
        return review_script({"verdict": "agree", "objections": [], "confidence_adjustment": 0})(n, kwargs)

    rec, client = run(monkeypatch, script)
    assert len(client.calls) == 2  # no reviewer call
    assert rec["review"]["reason"] == "review skipped: kill switch is on"


def test_reviewer_line_in_the_approval_box_and_report(payment_down, monkeypatch, capsys):
    rec, _ = run(monkeypatch, review_script({
        "verdict": "doubt", "confidence_adjustment": -0.1,
        "objections": [{"text": "cart also restarted twice", "evidence": ["E1"]}]}))
    report = open(rec["report_paths"]["report"], encoding="utf-8").read()
    assert "## Review" in report and "Verdict: **doubt**; confidence 0.9 -> 0.8" in report
    assert "Objection: cart also restarted twice (E1)" in report
    diag = {"review": rec["review"], "remediation_plan": rec["remediation_plan"], "status": "diagnosed",
            "confidence": 0.8}
    assert reviewer_line(diag) == "doubt: cart also restarted twice"
    assert reviewer_line({"review": {**rec["review"], "verdict": "agree"}}) == "agrees"
    assert reviewer_line({}) is None
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    capsys.readouterr()
    make_approver()(rec["remediation_plan"]["steps"][0], diag)
    assert "Reviewer   : doubt: cart also restarted twice" in capsys.readouterr().out


def test_review_tool_contract():
    props = REVIEW_TOOL["input_schema"]["properties"]
    assert props["verdict"]["enum"] == ["agree", "doubt", "disagree"]
    assert set(props) == {"verdict", "objections", "alternative_root_cause", "confidence_adjustment"}
