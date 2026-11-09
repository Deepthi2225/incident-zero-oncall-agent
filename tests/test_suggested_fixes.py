"""The agent's own suggested fixes: shown separately from the approved list, and never executed."""
import json

import pytest

from oncall import notify
from oncall.agent.diagnosis import SUBMIT_TOOL, canonical, validate
from oncall.agent.prompt import build_system_prompt
from oncall.evidence import registry
from oncall.report import suggestion_lines


def diagnose(*ideas, fault="container_stopped_or_restarting"):
    eid = registry.add("tool", "container", "seen")
    return validate({"headline": "h", "affected_services": [], "anomalies": [], "open_questions": [], "prevention": [],
                     "root_causes": [{"component": "payment", "fault_class": fault, "cause": "c", "confidence": 0.9,
                                      "status": "active", "evidence": [eid]}],
                     "proposed_action": {"action": "restart_service", "target": "payment", "reason": "r"},
                     "suggested_fixes": [{"evidence": [eid], **idea} for idea in ideas]})


IDEA = {"component": "payment", "fix": "Renew the certificate", "steps": ["Issue it", "Reload it"],
        "why_better": "A restart keeps the expired one", "risk": "A short blip", "confidence": 0.8}


def test_a_suggestion_is_kept_next_to_the_approved_fix(mem):
    diag = diagnose(IDEA)
    assert diag["suggested_fixes"][0]["fix"] == "Renew the certificate"
    assert [s["action"] for s in diag["remediation_plan"]["steps"]] == ["restart_service"]


def test_a_suggestion_can_never_become_a_step(mem):
    diag = diagnose({**IDEA, "fix": "docker rm -f payment", "action": "restart_service", "target": "checkout"})
    assert [s["target"] for s in diag["remediation_plan"]["steps"]] == ["payment"]
    assert "action" not in diag["suggested_fixes"][0] and "target" not in diag["suggested_fixes"][0]


def test_a_suggestion_when_nothing_on_the_list_fits(mem):
    diag = diagnose(IDEA, fault="unknown")
    assert not diag["remediation_plan"]["steps"] and diag["suggested_fixes"]


def test_a_suggestion_without_real_evidence_is_dropped(mem):
    assert diagnose({**IDEA, "evidence": ["E999"]})["suggested_fixes"] == []
    assert any("suggested fix" in n for n in diagnose({**IDEA, "evidence": []})["validation"]["notes"])


def test_odd_values_never_crash_and_are_limited(mem):
    diag = diagnose({**IDEA, "steps": "not a list", "confidence": "high", "risk": None, "fix": "x" * 5000})
    found = diag["suggested_fixes"][0]
    assert found["steps"] == [] and found["confidence"] == 0.0 and found["risk"] == "not assessed" and len(found["fix"]) == 600
    assert diagnose({**IDEA, "fix": 7})["suggested_fixes"] == []


def test_none_is_fine(mem):
    assert diagnose()["suggested_fixes"] == []


def test_the_model_is_told_it_is_advice_only():
    assert "suggested_fixes" in SUBMIT_TOOL["input_schema"]["properties"]
    assert "suggested_fixes" in SUBMIT_TOOL["input_schema"]["required"]
    assert "ever executed" in build_system_prompt(10)


def test_alerts_show_it_marked_as_advice(mem):
    lines = notify._suggested(diagnose(IDEA))
    assert "will never run this" in lines[1] and any("Renew the certificate" in x for x in lines)
    assert notify._suggested({}) == []


def test_report_lines_include_why_and_risk(mem):
    text = "\n".join(suggestion_lines(diagnose(IDEA)))
    assert "Step 2: Reload it" in text and "Why it is better" in text and "Risk: A short blip" in text


# --- better diagnoses --------------------------------------------------------------------------------------

def many(*causes, affected=()):
    return validate({"headline": "h", "affected_services": list(affected), "anomalies": [], "open_questions": [], "prevention": [],
                     "root_causes": list(causes), "proposed_action": {"action": "escalate_to_human", "target": "x", "reason": "r"}})


def rc(component, evidence, fault="service_error", confidence=0.9):
    return {"component": component, "fault_class": fault, "cause": "c", "confidence": confidence, "status": "active", "evidence": evidence}


@pytest.mark.parametrize("given,expected", [("CurrencyService", "currency"), ("oteldemo.CurrencyService", "currency"), ("currency service", "currency"),
                                            ("Product Catalog", "product-catalog"), ("valkey_cart", "valkey-cart"), ("something-new", "something-new"), (7, 7)])
def test_service_names_become_the_shops_own(given, expected):
    assert canonical(given) == expected


def test_a_diagnosis_uses_the_shops_names(mem):
    e = registry.add("t", "trace", "x")
    assert many(rc("PaymentService", [e]))["root_causes"][0]["component"] == "payment"


def test_a_caller_sharing_evidence_is_a_victim_not_a_cause(mem):
    shared, own = registry.add("t", "trace", "x"), registry.add("t", "log", "y")
    diag = many(rc("CurrencyService", [shared, own]), rc("checkout", [shared]))
    assert [c["component"] for c in diag["root_causes"]] == ["currency"]
    assert diag["affected_services"][0]["service"] == "checkout" and any("victim" in n for n in diag["validation"]["notes"])


def test_a_caller_with_its_own_evidence_stays_a_cause(mem):
    one, two = registry.add("t", "trace", "x"), registry.add("t", "trace", "y")
    assert len(many(rc("payment", [one]), rc("checkout", [two]))["root_causes"]) == 2


def test_unrelated_services_sharing_evidence_both_stay(mem):
    shared = registry.add("t", "trace", "x")
    assert len(many(rc("payment", [shared]), rc("ad", [shared]))["root_causes"]) == 2


def test_unreachable_without_checking_the_container_is_limited(mem):
    trace, box = registry.add("t", "trace", "x"), registry.add("t", "container", "y")
    assert many(rc("currency", [trace], "service_unreachable", 0.95))["root_causes"][0]["confidence"] == 0.75
    assert many(rc("currency", [trace, box], "service_unreachable", 0.95))["root_causes"][0]["confidence"] == 0.95


# --- asking for a suggestion ---------------------------------------------------------------------------------

def _run(monkeypatch, payment_down, third):
    from tests.conftest import FIXTURES
    from tests.test_agent_loop import block_tool, make_agent, response
    seen = {}

    def script(n, kwargs):
        if n == 1:
            return response(block_tool("h", "get_service_health", {"window": "5m"}))
        if n == 2:
            for part in kwargs["messages"][-1]["content"]:
                seen["e"] = json.loads(part["content"])["flagged"][0]["evidence"]
            return response(block_tool("s", "submit_diagnosis", {"headline": "h", "affected_services": [], "anomalies": [], "open_questions": [], "prevention": [],
                                                                  "root_causes": [rc("payment", [seen["e"]], "unknown", 0.9)],
                                                                  "proposed_action": {"action": "escalate_to_human", "target": "payment", "reason": "r"}}))
        return third(seen["e"])
    agent, client = make_agent(monkeypatch, script)
    return agent.run(json.loads((FIXTURES / "payment_down" / "alert.json").read_text())), client


def test_it_asks_once_when_nothing_on_the_list_fits(payment_down, monkeypatch):
    from tests.test_agent_loop import block_tool, response

    def third(e):
        return response(block_tool("s2", "submit_diagnosis", {"headline": "changed", "root_causes": [], "suggested_fixes": [{**IDEA, "evidence": [e]}]}))
    record, client = _run(monkeypatch, payment_down, third)
    assert len(client.calls) == 3 and record["investigation"]["outcome"] == "submitted"
    assert record["diagnosis"]["suggested_fixes"][0]["fix"] == "Renew the certificate"
    assert record["diagnosis"]["headline"] == "h" and record["diagnosis"]["root_causes"][0]["component"] == "payment"  # only suggestions change


def test_nothing_to_add_keeps_the_diagnosis(payment_down, monkeypatch):
    from types import SimpleNamespace

    from tests.test_agent_loop import response
    record, _ = _run(monkeypatch, payment_down, lambda e: response(SimpleNamespace(type="text", text="nothing more"), stop="end_turn"))
    assert record["investigation"]["outcome"] == "submitted" and record["diagnosis"]["root_causes"]
