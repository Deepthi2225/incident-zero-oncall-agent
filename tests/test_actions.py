"""Remedy choice by fault class, the Action API allowlist, execution gating, prevention, report."""
import pytest

from oncall import config
from oncall.agent import actions
from oncall.agent.diagnosis import PROPOSABLE_ACTIONS, SUBMIT_TOOL, validate
from oncall.agent.prompt import build_system_prompt
from oncall.evidence import registry
from oncall.remediate import make_approver
from oncall.report import build_report, to_organiser_schema


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    registry.reset()
    monkeypatch.setattr(config, "ALLOW_FLAG_TOGGLE", False)
    monkeypatch.setattr(config, "ALLOWED_FLAG_NAMES", [])
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", tmp_path / "STOP")


def diagnose(fault_class, component="payment", proposed=None, confidence=0.9, prevention=None):
    eid = registry.add("t", "trace", f"{component} evidence")
    diag = {
        "headline": "h", "affected_services": [], "anomalies": [],
        "root_causes": [{"component": component, "fault_class": fault_class, "cause": "c",
                         "confidence": confidence, "evidence": [eid]}],
        "proposed_action": proposed or {"action": "restart_service", "target": component, "reason": "r"},
        "open_questions": [],
    }
    if prevention is not None:
        diag["prevention"] = prevention
    return validate(diag)


# --- allowlist -------------------------------------------------------------------------

def test_allowlist_is_exactly_the_four_action_api_actions():
    assert list(actions.ACTIONS) == ["restart_service", "toggle_feature_flag",
                                     "throttle_load_generator", "scale_service"]
    assert actions.ACTIONS["scale_service"]["available"] is False
    assert "scale_service" not in PROPOSABLE_ACTIONS
    enum = SUBMIT_TOOL["input_schema"]["properties"]["proposed_action"]["properties"]["action"]["enum"]
    assert enum == ["restart_service", "toggle_feature_flag", "throttle_load_generator",
                    "escalate_to_human"]


def test_rollback_inverses():
    assert actions.rollback_for("restart_service") is None
    assert actions.rollback_for("toggle_feature_flag")["action"] == "toggle_feature_flag"
    assert actions.rollback_for("throttle_load_generator")["note"] == "remove the throttle"


# --- fault class -> remedy -----------------------------------------------------------------

@pytest.mark.parametrize("fault_class,component", [
    ("container_stopped_or_restarting", "payment"),
    ("dependency_down", "valkey-cart"),
])
def test_restart_on_the_root_cause_component(fault_class, component):
    # The model asked to restart a victim; the remedy targets the root-cause component.
    s = diagnose(fault_class, component, {"action": "restart_service", "target": "checkout", "reason": "r"})
    act = s["proposed_action"]
    assert act["action"] == "restart_service" and act["target"] == component
    assert act["rollback"] is None and act["dry_run"] is True


def test_traffic_flood_throttles_the_load_generator():
    s = diagnose("traffic_flood", "frontend-proxy",
                 {"action": "restart_service", "target": "frontend", "reason": "r"})
    act = s["proposed_action"]
    assert act["action"] == "throttle_load_generator" and act["target"] == "load-generator"
    assert act["rollback"]["note"] == "remove the throttle"
    assert any("replaced by throttle_load_generator" in n for n in s["validation"]["notes"])


@pytest.mark.parametrize("fault_class", ["network_degradation", "resource_starvation",
                                         "service_unreachable", "unknown"])
def test_no_allowlisted_fix_escalates_instead_of_restarting(fault_class):
    s = diagnose(fault_class, "cart", {"action": "restart_service", "target": "cart", "reason": "try it"})
    act = s["proposed_action"]
    assert s["status"] == "diagnosed"  # the diagnosis stands; only the remedy is a human's call
    assert act["action"] == "escalate_to_human" and act["target"] == "cart"
    assert act["next_steps"] == actions.NEXT_STEPS[fault_class]
    assert "rollback" not in act
    assert any("replaced by escalate_to_human" in n for n in s["validation"]["notes"])


@pytest.mark.parametrize("fault_class", ["service_error", "input_specific_error", "memory_leak",
                                         "cpu_or_gc_pressure"])
def test_app_faults_escalate_while_flag_toggle_is_off(fault_class):
    s = diagnose(fault_class, "ad", {"action": "toggle_feature_flag", "target": "ad",
                                     "flag": "someFlag", "reason": "r"})
    act = s["proposed_action"]
    assert act["action"] == "escalate_to_human" and "disabled" in act["reason"]
    assert "flag" not in act


def test_flag_toggle_needs_the_switch_and_an_allowed_name(monkeypatch):
    monkeypatch.setattr(config, "ALLOW_FLAG_TOGGLE", True)
    monkeypatch.setattr(config, "ALLOWED_FLAG_NAMES", ["allowedFlag"])
    ok = diagnose("service_error", "payment", {"action": "toggle_feature_flag", "target": "payment",
                                               "flag": "allowedFlag", "reason": "r"})
    assert ok["proposed_action"]["action"] == "toggle_feature_flag"
    assert ok["proposed_action"]["flag"] == "allowedFlag"
    assert ok["proposed_action"]["rollback"]["action"] == "toggle_feature_flag"

    for flag in ("otherFlag", None):
        bad = diagnose("service_error", "payment", {"action": "toggle_feature_flag", "target": "payment",
                                                    "flag": flag, "reason": "r"})
        assert bad["proposed_action"]["action"] == "escalate_to_human"
        assert "not in the allowed flag names" in bad["proposed_action"]["reason"]


def test_scale_service_is_never_proposed():
    s = diagnose("resource_starvation", "checkout",
                 {"action": "scale_service", "target": "checkout", "reason": "more replicas"})
    assert s["proposed_action"]["action"] == "escalate_to_human"


def test_low_confidence_escalates_even_when_a_fix_exists():
    s = diagnose("container_stopped_or_restarting", confidence=0.4)
    assert s["status"] == "escalate" and s["proposed_action"]["action"] == "escalate_to_human"
    assert s["proposed_action"]["next_steps"]


def multi(*rcs, proposed=None):
    root_causes = []
    for component, fault_class, confidence in rcs:
        eid = registry.add("t", "trace", f"{component} evidence")
        root_causes.append({"component": component, "fault_class": fault_class, "cause": "c",
                            "confidence": confidence, "evidence": [eid]})
    return validate({"headline": "h", "affected_services": [], "anomalies": [], "open_questions": [],
                     "root_causes": root_causes,
                     "proposed_action": proposed or {"action": "restart_service", "target": "payment",
                                                     "reason": "r"}})


def test_plan_has_one_step_per_fixable_root_cause_dependencies_first():
    s = multi(("payment", "container_stopped_or_restarting", 0.9),
              ("valkey-cart", "dependency_down", 0.8),
              ("cart", "network_degradation", 0.7))
    plan = s["remediation_plan"]
    # valkey-cart is a dependency (of cart, of checkout...), so it goes first despite lower confidence.
    assert [(st["step"], st["action"], st["target"]) for st in plan["steps"]] == [
        (1, "restart_service", "valkey-cart"), (2, "restart_service", "payment")]
    assert [e["target"] for e in plan["escalations"]] == ["cart"]
    assert plan["escalations"][0]["next_steps"] == actions.NEXT_STEPS["network_degradation"]
    assert s["proposed_action"]["target"] == "valkey-cart"


def test_plan_orders_db_before_its_callers_and_dedupes():
    s = multi(("product-catalog", "container_stopped_or_restarting", 0.9),
              ("astronomy-db", "dependency_down", 0.85),
              ("astronomy-db", "dependency_down", 0.7))
    assert [st["target"] for st in s["remediation_plan"]["steps"]] == ["astronomy-db", "product-catalog"]


def test_low_confidence_root_cause_is_escalated_not_acted_on():
    s = multi(("payment", "container_stopped_or_restarting", 0.9), ("cart", "dependency_down", 0.5))
    assert [st["target"] for st in s["remediation_plan"]["steps"]] == ["payment"]
    assert "below 0.6" in s["remediation_plan"]["escalations"][0]["reason"]


@pytest.mark.parametrize("fault_class", ["memory_leak", "cpu_or_gc_pressure"])
def test_restart_is_only_a_human_option_for_leaks_and_cpu(fault_class):
    s = diagnose(fault_class, "recommendation")
    assert s["remediation_plan"]["steps"] == []  # never restarted automatically
    steps = s["remediation_plan"]["escalations"][0]["next_steps"]
    assert any("restart the service as a temporary mitigation" in n for n in steps)


# --- prevention, approval, execution gate -------------------------------------------------

def test_prevention_from_model_or_fault_class_default():
    assert diagnose("dependency_down", prevention=["Alert on valkey down"])["prevention"] == \
        ["Alert on valkey down"]
    assert diagnose("dependency_down")["prevention"] == actions.PREVENTION["dependency_down"]
    assert diagnose("dependency_down", prevention=["", 3])["prevention"] == \
        actions.PREVENTION["dependency_down"]


def test_auto_approval_is_dry_run_only():
    s = diagnose("container_stopped_or_restarting")
    step = s["remediation_plan"]["steps"][0]
    step["approval"] = make_approver(auto_approve=True)(step, s)
    assert step["approval"]["status"] == "approved" and step["approval"]["approved_by"] == "auto"
    assert actions.execution_allowed(step) == (False, "auto-approval is dry-run only")


def test_execution_needs_a_human_yes_and_no_kill_switch():
    s = diagnose("container_stopped_or_restarting")
    step = s["remediation_plan"]["steps"][0]
    step["approval"] = make_approver(interactive=False)(step, s)
    assert actions.execution_allowed(step) == (False, "not approved (status pending)")
    step["approval"] = {"status": "approved", "approved_by": "human"}
    assert actions.execution_allowed(step) == (True, "approved by a human")
    config.KILL_SWITCH_FILE.write_text("stop")
    assert actions.execution_allowed(step) == (False, "kill switch is on")

    esc = diagnose("network_degradation")["proposed_action"]
    esc["approval"] = {"status": "approved", "approved_by": "human"}
    assert actions.execution_allowed(esc)[0] is False  # escalation is not an Action API call


# --- prompt and report ----------------------------------------------------------------------

def test_prompt_states_the_mapping():
    prompt = build_system_prompt()
    assert "scale_service is UNAVAILABLE" in prompt
    assert "container_stopped_or_restarting, dependency_down -> restart_service" in prompt
    assert "traffic_flood -> throttle_load_generator" in prompt
    assert "(flag toggling is disabled)" in prompt
    assert "Never restart something just to try it" in prompt


def test_report_has_timeline_root_cause_evidence_action_and_prevention():
    s = diagnose("container_stopped_or_restarting")
    record = {"run_id": "r1", "source": "test", "outcome": "submitted",
              "alert": {"detected_at": "2026-10-24T09:00:00+00:00",
                        "trigger": [{"service": "checkout", "signal": "error_rate", "value": 1.0}]},
              "started_at": "2026-10-24T09:00:05+00:00", "diagnosed_at_s": 20.0,
              "steps": [{"step": 1, "tool": "get_service_health", "input": {"window": "5m"},
                         "at_s": 1.2, "is_error": False}],
              "evidence": registry.dump(), "summary": s}
    md = build_report(record)
    for heading in ("## Timeline", "## Root cause", "## Evidence", "## Actions taken",
                    "## What would prevent a repeat"):
        assert heading in md
    assert "Alert: checkout error_rate=1.0" in md and "+1.2s | get_service_health(window='5m')" in md
    assert "No action was executed" in md and "### Step 1: restart_service on `payment`" in md
    org = to_organiser_schema(record)
    assert org["actions"] == [{"action": "restart_service", "target": "payment", "result": "not run",
                               "verify": None}]
    assert org["schema"].startswith("UNCONFIRMED")
    assert actions.PREVENTION["container_stopped_or_restarting"][0] in md
    assert "payment evidence" in md  # cited evidence quoted from the registry
