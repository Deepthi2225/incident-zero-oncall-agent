"""Several faults at once, faults the agent does not recognise, and alerts that arrive during a run."""
import threading

import detector
from oncall import notify
from oncall.agent.diagnosis import CONFIDENT, check_coverage, validate
from oncall.agent.run import step_budget
from oncall.evidence import registry


def cause(component, fault_class="service_error", evidence=None, confidence=0.9):
    return {"component": component, "fault_class": fault_class, "cause": "c", "confidence": confidence,
            "status": "active", "evidence": evidence}


def diagnose(*causes, ruled_out=()):
    return validate({"headline": "h", "affected_services": [], "anomalies": [], "root_causes": list(causes),
                     "ruled_out": list(ruled_out), "proposed_action": {"action": "restart_service", "target": "x",
                                                                       "reason": "r"},
                     "prevention": [], "open_questions": []})


def evidence(kind="trace"):
    return [registry.add("tool", kind, "seen")]


# --- turn budget --------------------------------------------------------------------------------------

def test_budget_grows_with_alerting_services_and_is_capped():
    assert step_budget({"service": "checkout"}, 10) == 10
    assert step_budget({"alerting_services": ["a", "b", "c"]}, 10) == 14
    assert step_budget({"alerting_services": [str(i) for i in range(30)]}, 10) == 20


# --- every alerting service is accounted for ----------------------------------------------------------

def test_two_independent_faults_are_both_covered(mem):
    diag = diagnose(cause("payment", evidence=evidence()), cause("recommendation", evidence=evidence()))
    assert check_coverage(diag, {"alerting_services": ["payment", "recommendation"]}) == []


def test_a_victim_of_the_root_cause_is_covered(mem):
    assert check_coverage(diagnose(cause("payment", evidence=evidence())), {"alerting_services": ["checkout"]}) == []


def test_an_unrelated_alert_is_escalated_as_a_possible_second_fault(mem):
    diag = diagnose(cause("payment", "container_stopped_or_restarting", evidence("container")))
    assert check_coverage(diag, {"alerting_services": ["checkout", "ad"]}) == ["ad"]
    item = diag["remediation_plan"]["escalations"][-1]
    assert item["target"] == "ad" and item["source"] == "unexplained alert" and "ad" in diag["open_questions"][-1]
    assert diag["remediation_plan"]["steps"]  # the proven fix is still offered, for approval


def test_a_ruled_out_service_is_covered(mem):
    diag = diagnose(cause("payment", evidence=evidence()),
                    ruled_out=[{"service": "ad", "reason": "brief blip", "evidence": evidence("metric")}])
    assert check_coverage(diag, {"alerting_services": ["ad"]}) == []


def test_a_traffic_flood_explains_every_service(mem):
    diag = diagnose(cause("frontend-proxy", "traffic_flood", evidence("metric")))
    assert check_coverage(diag, {"alerting_services": ["payment", "ad", "quote"]}) == []


def test_no_root_cause_adds_nothing_extra(mem):
    diag = diagnose()
    assert check_coverage(diag, {"alerting_services": ["ad"]}) == []


def test_an_unexplained_alert_notifies_a_human():
    record = {"investigation": {"outcome": "submitted"}, "diagnosis": {"status": "diagnosed"}, "metadata": {},
              "escalations": [{"source": "unexplained alert", "reason": "ad alerted"}], "outcome": {}}
    assert ("unexplained_alert", "ad alerted") in notify.events_for(record)


# --- unfamiliar faults -------------------------------------------------------------------------------

def test_a_label_without_its_kind_of_evidence_is_capped_and_escalated(mem):
    diag = diagnose(cause("ad", "memory_leak", evidence("log")))
    assert diag["root_causes"][0]["confidence"] < CONFIDENT and diag["status"] == "escalate"
    assert not diag["remediation_plan"]["steps"] and any("capped" in n for n in diag["validation"]["notes"])


def test_a_label_with_its_kind_of_evidence_is_kept(mem):
    diag = diagnose(cause("ad", "memory_leak", evidence("metric")))
    assert diag["root_causes"][0]["confidence"] == 0.9


def test_unknown_fault_class_is_never_offered_a_fix(mem):
    diag = diagnose(cause("ad", "unknown", evidence("log")))
    assert not diag["remediation_plan"]["steps"] and diag["remediation_plan"]["escalations"]


def test_a_made_up_fault_class_becomes_unknown(mem):
    assert diagnose(cause("ad", "gremlins", evidence("log")))["root_causes"][0]["fault_class"] == "unknown"


# --- alerts during a run ------------------------------------------------------------------------------

def breach(service):
    return {"service": service, "signal": "error_rate", "value": 0.5, "threshold": 0.1, "severity": 5.0}


def run_detector(monkeypatch, polls_while_busy, later, handler_polls=1):
    """The detector with one payment incident; polls during it return polls_while_busy, then later."""
    busy, seen, done = threading.Event(), [], threading.Event()
    def poll(baseline, peaks=None):
        if not seen:
            return [breach("payment")], {"rps": {}}
        return (polls_while_busy if busy.is_set() else later), {"rps": {}}

    def handler(incident):
        seen.append(incident)
        if len(seen) == 1:
            busy.set()
            done.wait(0.3)  # the watcher polls meanwhile
            busy.clear()
            return {"diagnosis": {"root_causes": [{"component": "payment", "fault_class": "service_error"}]}}
        raise SystemExit  # enough: the second incident arrived

    monkeypatch.setattr(detector, "poll", poll)
    monkeypatch.setattr(detector, "load_baseline", lambda: {})
    monkeypatch.setattr(detector, "CONSECUTIVE", 1)
    monkeypatch.setattr(detector, "POLL_SECS", 0.02)
    return handler, seen


def test_an_alert_during_a_run_is_queued_and_grouped_after_it(monkeypatch, capsys):
    now = [breach("ad"), breach("quote")]
    handler, seen = run_detector(monkeypatch, now, now)
    try:
        detector.run(on_incident=handler)
    except SystemExit:
        pass
    assert seen[1]["alerting_services"] == ["ad", "quote"]
    assert seen[1]["earlier_incident"]["root_causes"][0]["component"] == "payment"
    assert "Queued for afterwards" in capsys.readouterr().out


def test_a_queued_alert_that_recovered_is_dropped(monkeypatch):
    handler, seen = run_detector(monkeypatch, [breach("ad")], [])

    def stop_soon(incident):
        result = handler(incident)
        monkeypatch.setattr(detector.time, "sleep", lambda s: (_ for _ in ()).throw(SystemExit))
        return result
    try:
        detector.run(on_incident=stop_soon)
    except SystemExit:
        pass
    assert len(seen) == 1
