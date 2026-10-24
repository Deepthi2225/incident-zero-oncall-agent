"""The read-only live dashboard: endpoints over complete, partial, interrupted and Day 1 records; no flag
or secret text in any response; text is never parsed as HTML; localhost only; GET only."""
import json
import shutil
import threading
import urllib.error
import urllib.request

import pytest

from oncall import config
from oncall.record import FLAG_TEXT
from oncall.ui import data
from oncall.ui.server import PAGE, make_server, route
from tests.schema_check import errors
from tests.test_incident_record import fake_run

SECRET = "sk-ant-api03-UISECRETUISECRETUISECRET"


@pytest.fixture
def runs(payment_down, monkeypatch):
    """A runs/ folder with one record of each kind."""
    complete = fake_run(monkeypatch)
    folder = config.RUNS_DIR
    rec = json.loads(open(complete["path"], encoding="utf-8").read())

    partial = json.loads(json.dumps(rec))  # still running: step 1 waiting at the terminal prompt
    partial["status"], partial["outcome"] = "running", None
    partial["metadata"]["run_id"] = "20991231-000001"
    partial["remediation_plan"]["steps"][0]["approval"] = {"status": "awaiting", "since": "2099-12-31T00:00:01+00:00"}
    partial["remediation_plan"]["steps"][0].pop("execution", None)
    (folder / "20991231-000001-test.json").write_text(json.dumps(partial))

    interrupted = json.loads(json.dumps(rec))
    interrupted.update(status="interrupted", diagnosis=None, remediation_plan=None, outcome=None, escalations=[])
    interrupted["metadata"]["run_id"] = "20991231-000002"
    (folder / "20991231-000002-test.json").write_text(json.dumps(interrupted))

    shutil.copy(config.ROOT / "results" / "checkpoint2" / "stop-payment.json", folder / "20261023-175355.json")

    nasty = json.loads(json.dumps(rec))  # what a record should never contain, written by hand
    nasty["metadata"]["run_id"] = "20991231-000003"
    nasty["diagnosis"]["headline"] = '<script>alert("x")</script><img src=x onerror=alert(1)>'
    nasty["alert"]["note"] = f"key {SECRET} and Bearer abcdef1234567890"
    nasty["evidence"][0]["observation"] = "feature_flag.paymentFailure evaluated to true"
    (folder / "20991231-000003-test.json").write_text(json.dumps(nasty))
    return {"complete": rec["metadata"]["run_id"], "partial": "20991231-000001",
            "interrupted": "20991231-000002", "old": "20261023-175355", "nasty": "20991231-000003"}


def body(path):
    status, ctype, raw = route(path)
    return status, ctype, raw.decode("utf-8")


def assert_clean(text):
    assert "sk-ant-" not in text and "UISECRET" not in text and "abcdef1234567890" not in text
    assert not FLAG_TEXT.search(text) and "flagd" not in text.lower()


def test_incidents_list_every_kind(runs):
    status, ctype, text = body("/api/incidents")
    assert status == 200 and ctype.startswith("application/json")
    rows = {r["run_id"]: r for r in json.loads(text)["incidents"]}
    assert set(runs.values()) <= set(rows)
    assert rows[runs["partial"]]["outcome"] == "running" and rows[runs["interrupted"]]["outcome"] == "interrupted"
    assert rows[runs["old"]]["root_cause"] == "payment"
    assert_clean(text)


@pytest.mark.parametrize("kind", ["complete", "partial", "interrupted", "old", "nasty"])
def test_each_record_renders_without_errors(runs, kind):
    status, _, text = body(f"/api/incident/{runs[kind]}")
    assert status == 200
    record = json.loads(text)
    assert record["schema_version"] == 2 and record["ui"]["file"].startswith(runs[kind])
    assert errors({k: v for k, v in record.items() if k != "ui"}) == []
    assert_clean(text)
    if kind == "partial":
        assert record["ui"]["awaiting_approval"] == [1] and record["ui"]["in_progress"] is True
    if kind == "interrupted":
        assert record["status"] == "interrupted" and record["diagnosis"] is None


def test_newest_incident_and_unknown_ids(runs):
    assert body("/api/incident")[0] == 200
    for bad in ("/api/incident/..%2F..%2Fetc%2Fpasswd", "/api/incident/nope", "/api/report/..%5C.env"):
        assert body(bad)[0] == 404


def test_report_is_plain_text(runs):
    status, ctype, text = body(f"/api/report/{runs['complete']}")
    assert status == 200 and ctype.startswith("text/plain") and "# Incident report" in text
    assert_clean(text)


def test_actions_and_status(runs, monkeypatch):
    monkeypatch.setattr(config, "ACTION_API_URL", "http://127.0.0.1:1")  # nothing listens there
    status, _, text = body("/api/actions")
    logs = json.loads(text)
    assert status == 200 and logs["agent"] and logs["action_api"] == []
    s = json.loads(body("/api/status")[2])
    assert s["kill_switch"] is False and s["action_api"]["reachable"] is False and s["reviewer"] in ("on", "off")
    config.KILL_SWITCH_FILE.write_text("stop")
    assert json.loads(body("/api/status")[2])["kill_switch"] is True


def test_health_grid_uses_the_detector_limits(mem, monkeypatch):
    det = data._detector()
    baseline = {"checkout": {"p95_ms": 100.0, "rps": 0.4}, "payment": {"p95_ms": 30.0, "rps": 0.4},
                "cart": {"p95_ms": 9.0, "rps": 2.0}}
    snap = {"rps": {"checkout": 0.4, "payment": 0.05, "cart": 2.0, "flagd": 3.0},
            "error_rate": {"checkout": 0.6, "payment": 0.0, "cart": 0.03}, "p95_ms": {"checkout": 120.0, "cart": 8.0}}
    monkeypatch.setattr(det, "load_baseline", lambda: baseline)
    monkeypatch.setattr(det, "poll", lambda b, peaks=None: ([{"service": "checkout", "signal": "error_rate"}], snap))
    rows = {r["service"]: r for r in data.health(force=True)["services"]}
    assert "flagd" not in rows  # flag services never appear
    assert rows["checkout"]["state"] == "red" and rows["cart"]["state"] == "amber"  # 3% errors: above half the limit
    assert rows["payment"]["state"] == "amber"  # traffic near its silence limit
    monkeypatch.setattr(det, "poll", lambda b, peaks=None: (_ for _ in ()).throw(OSError("down")))
    assert "not reachable" in data.health(force=True)["error"]


def test_page_never_parses_record_text_as_html_and_loads_nothing_external():
    page = PAGE.read_text(encoding="utf-8")
    assert "innerHTML" not in page and "outerHTML" not in page and "eval(" not in page
    assert "document.write" not in page and "<script src" not in page
    assert "http://" not in page and "https://" not in page  # works offline, no CDN
    assert "<button" not in page and "method=\"post\"" not in page.lower()  # nothing acts


def test_server_binds_localhost_only_and_is_get_only(runs):
    with pytest.raises(ValueError, match="localhost only"):
        make_server("0.0.0.0", 0)
    server = make_server(port=0)
    assert server.server_address[0] == "127.0.0.1"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{base}/api/incident/{runs['nasty']}") as resp:
            assert resp.headers["X-Content-Type-Options"] == "nosniff"
            assert resp.headers["Content-Type"].startswith("application/json")
            assert_clean(resp.read().decode())
        with urllib.request.urlopen(base + "/") as resp:
            assert "On-call agent" in resp.read().decode()
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(base + "/api/status", data=b"{}", method="POST"))
        assert e.value.code == 405
    finally:
        server.shutdown()
        server.server_close()


def test_awaiting_approval_is_saved_while_the_human_decides(payment_down, monkeypatch):
    seen = {}

    def approver(step, diag):
        newest = sorted(config.RUNS_DIR.glob("*-test.json"))[-1]
        rec = json.loads(newest.read_text())
        seen["approval"] = rec["remediation_plan"]["steps"][0]["approval"]
        seen["errors"] = errors(rec)
        return {"status": "approved", "approved_by": "human"}

    rec = fake_run(monkeypatch, approve=approver)
    assert seen["approval"]["status"] == "awaiting" and seen["approval"]["since"] and seen["errors"] == []
    assert rec["remediation_plan"]["steps"][0]["approval"]["status"] == "approved"  # replaced by the decision
