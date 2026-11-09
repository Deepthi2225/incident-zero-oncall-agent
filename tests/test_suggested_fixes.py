"""The agent's own suggested fixes: shown separately from the approved list, and never executed."""
from oncall import notify
from oncall.agent.diagnosis import SUBMIT_TOOL, validate
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
    assert "suggested_fixes" not in SUBMIT_TOOL["input_schema"]["required"]
    assert "ever executed" in build_system_prompt(10)


def test_alerts_show_it_marked_as_advice(mem):
    lines = notify._suggested(diagnose(IDEA))
    assert "will never run this" in lines[1] and any("Renew the certificate" in x for x in lines)
    assert notify._suggested({}) == []


def test_report_lines_include_why_and_risk(mem):
    text = "\n".join(suggestion_lines(diagnose(IDEA)))
    assert "Step 2: Reload it" in text and "Why it is better" in text and "Risk: A short blip" in text
