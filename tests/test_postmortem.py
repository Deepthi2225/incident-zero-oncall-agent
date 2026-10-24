"""The draft postmortem (oncall/postmortem.py): derived only from the incident record, stored under
"postmortem" (schema-valid), rendered in the report and the dashboard. A recovered run, an
escalated run, and old-format or partial records (missing values shown as "unknown")."""
import json
from pathlib import Path

import pytest

from oncall import config
from oncall.action_api import FakeClient
from oncall.postmortem import UNKNOWN_WHY, build_postmortem, parse_time
from oncall.record import load, save_existing, upgrade
from oncall.report import build_report
from oncall.ui.server import route
from oncall.verify import NOT_RECOVERED, RECOVERED
from tests.schema_check import errors
from tests.test_incident_record import ALERT, fake_run
from tests.test_notify import edited, low, run

DETECTED = "2026-10-23T07:20:20+00:00"     # the payment_down fixture's container stopped at 07:18:20.5
VERIFIED = "2026-10-23T07:26:00+00:00"


class TimedVerifier:
    """Recovered after 62 s, with one timestamped check (what a live Verifier records)."""

    def measure(self, services):
        return {}

    def verify(self, step, services, before=None, progress=None):
        return {"outcome": RECOVERED, "seconds": 62.0, "unhealthy": [], "evidence": [], "services": {},
                "checks": [{"poll": 1, "at": VERIFIED, "ok": True, "passed": 2, "judged": 2}]}


def approve(step, diag):
    return {"status": "approved", "approved_by": "human", "decision_at": "2026-10-23T07:22:00+00:00"}


def saved(rec):
    return json.loads(Path(rec["path"]).read_text(encoding="utf-8"))


def test_recovered_run_has_a_complete_postmortem(payment_down, monkeypatch):
    rec = fake_run(monkeypatch, client=FakeClient(live=True), verifier=TimedVerifier(), approve=approve,
                   alert={**ALERT, "detected_at": DETECTED})
    data = saved(rec)
    assert errors(data) == [] and data["postmortem"] == rec["postmortem"]
    pm = data["postmortem"]
    t, d = pm["times"], pm["durations"]
    assert (t["fault_started"], t["fault_started_evidence"], t["fault_started_what"]) == \
        ("2026-10-23T07:18:20+00:00", "E1", "payment container stopped")
    assert (t["detected"], t["approved"], t["recovery_verified"]) == (DETECTED, "2026-10-23T07:22:00+00:00", VERIFIED)
    assert t["diagnosed"] and t["executed"] and t["investigation_started"]
    assert d["time_to_detect_s"] == 120.0 and d["time_to_recover_s"] == 340.0 and d["total_s"] == 460.0
    assert d["time_to_diagnose_s"] is not None

    assert pm["impact"]["services"] == [{"service": "checkout", "impact": "orders fail", "evidence": ["E1"]}]
    assert pm["impact"]["failed_requests_total"] is None             # never invented

    whys = pm["five_whys"]
    assert [w["source"] for w in whys] == ["derived", "derived", "derived", "unknown"]
    assert all(w["evidence"] for w in whys[:3]) and whys[-1]["because"] == UNKNOWN_WHY
    assert "payment [container_stopped_or_restarting]" in whys[1]["because"]
    assert "exit code 143" in whys[2]["because"]

    assert "Root cause found in 1 tool call(s)" in " ".join(pm["went_well"])
    assert any("verified as recovered within 62 s" in x for x in pm["went_well"])
    assert any("Detection took 120 s" in x for x in pm["went_well"])
    assert any("reviewer did not run" in x for x in pm["could_be_better"])
    assert pm["action_items"] == [{"action": "Alert on payment container exits.", "type": "detect",
                                   "priority": "high", "owner": "to assign"}]

    report = Path(rec["report_paths"]["report"]).read_text(encoding="utf-8")
    assert "## Postmortem (draft)" in report and "### 5 Whys (draft)" in report
    assert "| Fault started (earliest evidence) | 2026-10-23T07:18:20+00:00 (payment container stopped, E1) |" in report
    assert "| Time to recover (alert -> verified recovery) | 340.0 |" in report
    assert "| Alert on payment container exits. | detect | high | to assign |" in report
    assert f"4. Why did that happen? {UNKNOWN_WHY}" in report


def test_escalated_run_marks_what_is_unknown(payment_down, monkeypatch):
    rec = run(monkeypatch, script=edited(low))                       # confidence 0.3: escalated
    pm = saved(rec)["postmortem"]
    t, d = pm["times"], pm["durations"]
    assert t["detected"] is None and t["approved"] is None and t["executed"] is None and t["recovery_verified"] is None
    assert d["time_to_detect_s"] is None and d["time_to_recover_s"] is None and d["total_s"] is not None
    whys = pm["five_whys"]
    assert whys[0]["source"] == "derived" and whys[0]["evidence"]   # the impact is in the evidence
    assert whys[1]["because"] == UNKNOWN_WHY and whys[1]["evidence"] == []  # not confident: no root-cause why
    assert any("Confidence 0.3 stayed below the threshold" in x for x in pm["could_be_better"])
    assert any("detection time is not in the record" in x for x in pm["could_be_better"])
    report = Path(rec["report_paths"]["report"]).read_text(encoding="utf-8")
    assert "| Detected (alert) | unknown |" in report and "| Time to recover (alert -> verified recovery) | unknown |" in report


def test_failed_verify_and_rejection_are_listed(payment_down, monkeypatch):
    rec = run(monkeypatch, client=FakeClient(live=True), outcome=NOT_RECOVERED,
              alert={**ALERT, "incident_id": "INC-FAIL"})
    better = " ".join(saved(rec)["postmortem"]["could_be_better"])
    assert "verified as not_recovered" in better
    from tests.test_remediate import human
    rec = run(monkeypatch, approve=human({"payment": False}), alert={**ALERT, "incident_id": "INC-NO"})
    assert "rejected by the on-call engineer" in " ".join(saved(rec)["postmortem"]["could_be_better"])


@pytest.mark.parametrize("name", ["stop-payment.json", "cache-down.json", "db-down.json"])
def test_old_format_records_render_with_unknowns(name):
    record = upgrade(json.loads((config.ROOT / "results" / "checkpoint2" / name).read_text(encoding="utf-8")))
    assert "postmortem" not in record
    pm = build_postmortem(record)
    assert errors({**record, "postmortem": pm}) == []
    report = build_report(record)
    assert "## Postmortem (draft)" in report and "unknown" in report
    assert pm["times"]["detected"] is None or isinstance(pm["times"]["detected"], str)


def test_partial_and_empty_records_do_not_crash(payment_down, monkeypatch):
    rec = saved(fake_run(monkeypatch))
    rec.update(status="interrupted", diagnosis=None, remediation_plan=None, outcome=None, escalations=[],
               evidence=[], review=None)
    rec.pop("postmortem")
    pm = build_postmortem(rec)
    assert errors({**rec, "postmortem": pm}) == []
    assert set(pm["times"].values()) - {None} <= {rec["metadata"]["started_at"], rec["metadata"]["ended_at"]}
    assert pm["five_whys"] == [{"n": 1, "why": "Why did this incident happen?", "because": UNKNOWN_WHY,
                                "evidence": [], "source": "unknown"}]
    assert pm["action_items"] == [] and "## Postmortem (draft)" in build_report(rec)


def test_manual_verify_updates_the_recovery_time(payment_down, monkeypatch):
    rec = run(monkeypatch, client=FakeClient(live=True), outcome=NOT_RECOVERED)
    record = load(rec["path"])
    assert record["postmortem"]["times"]["recovery_verified"] is None
    record["manual_verifications"].append({"at": "2026-10-24T11:00:00+00:00", "outcome": RECOVERED, "targets": []})
    after = save_existing(record, rec["path"], log=lambda *_: None)
    assert after["postmortem"]["times"]["recovery_verified"] == "2026-10-24T11:00:00+00:00"
    assert errors(after) == []


def test_model_five_whys_keep_only_real_evidence(payment_down, monkeypatch):
    rec = saved(fake_run(monkeypatch))
    rec["diagnosis"]["five_whys"] = [
        {"why": "Why did checkout fail?", "because": "payment was down", "evidence": ["E1"]},
        {"why": "Why was payment down?", "because": "someone stopped it", "evidence": ["E404"]}]
    whys = build_postmortem(rec)["five_whys"]
    assert [(w["because"], w["evidence"], w["source"]) for w in whys] == [
        ("payment was down", ["E1"], "model"), (UNKNOWN_WHY, [], "model"), (UNKNOWN_WHY, [], "unknown")]


def test_impact_uses_only_numbers_in_the_evidence():
    record = {"metadata": {}, "diagnosis": {"status": "diagnosed", "affected_services": [], "anomalies": []},
              "evidence": [
                  {"id": "E2", "tool": "get_service_health", "data": {"row": {
                      "service": "checkout", "rps": 0.4, "error_rate": 0.5, "flags": ["errors", "traffic_drop"],
                      "rps_ratio": 0.2, "baseline": {"rps": 2.0, "error_rate": 0.0}}}},
                  {"id": "E3", "tool": "find_error_traces", "data": {"origin_service": "payment", "operation": "Charge",
                                                                     "count": 6}},
                  {"id": "E4", "tool": "search_logs", "data": {"service": "payment", "level": "error", "count": 41,
                                                               "first_seen": "2026-10-23T07:18:21.123456789Z"}}]}
    imp = build_postmortem(record)["impact"]
    assert imp["signals"] == [{"text": "checkout: error rate 50.0% (baseline 0.0%); traffic 0.4 rps against a "
                                       "baseline of 2.0 rps (20%)", "evidence": ["E2"]}]
    assert [f["evidence"] for f in imp["failed_requests"]] == [["E3"], ["E4"]]
    assert "6 failing trace(s)" in imp["failed_requests"][0]["text"] and imp["failed_requests_total"] is None


def test_parse_time_edge_cases():
    assert parse_time("2026-10-23T07:18:20.500000000Z").isoformat() == "2026-10-23T07:18:20.500000+00:00"
    assert parse_time("0001-01-01T00:00:00Z") is None and parse_time("yesterday") is None and parse_time(None) is None
    assert parse_time("2026-10-23T07:18:20").tzinfo is not None


def test_dashboard_shows_the_postmortem(payment_down, monkeypatch):
    rec = fake_run(monkeypatch)
    status, _, raw = route("/api/incident/" + rec["metadata"]["run_id"])
    assert status == 200 and json.loads(raw)["postmortem"]["draft"] is True
    record = saved(rec)                                       # an older record without one: derived
    record.pop("postmortem")
    record["metadata"]["run_id"] = "20991231-000009"
    (config.RUNS_DIR / "20991231-000009-test.json").write_text(json.dumps(record), encoding="utf-8")
    status, _, raw = route("/api/incident/20991231-000009")
    assert status == 200 and json.loads(raw)["postmortem"]["five_whys"]
    page = route("/")[2].decode()
    assert "postmortemView" in page and "innerHTML" not in page


def test_impossible_time_order_is_unknown_not_negative():
    from oncall.postmortem import durations
    d = durations({"fault_started": "2026-10-23T08:00:00+00:00", "detected": "2026-10-23T07:00:00+00:00",
                   "investigation_started": None, "diagnosed": None, "recovery_verified": None, "ended": None})
    assert d["time_to_detect_s"] is None and all(v is None or v >= 0 for v in d.values())
