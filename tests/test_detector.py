"""detector.py: server-span error rate, silent services, finite values, file locations."""
import json
import math
from pathlib import Path

import pytest

import detector


def test_error_rate_counts_only_server_spans():
    q = detector.QUERIES["error_rate"]
    numerator, denominator = q.split(" / ")
    assert 'span_kind="SPAN_KIND_SERVER"' in numerator and 'status_code="STATUS_CODE_ERROR"' in numerator
    assert 'span_kind="SPAN_KIND_SERVER"' in denominator
    assert "span_kind" not in detector.QUERIES["rps"]  # rps stays on all spans


def fake_snapshot(rps, err=None, p95=None):
    return lambda: {"rps": rps, "error_rate": err or {}, "p95_ms": p95 or {}}


def test_silent_service_breach(monkeypatch):
    monkeypatch.setattr(detector, "snapshot", fake_snapshot({"checkout": 0.4, "email": 0.001}))
    breaches, _ = detector.poll(baseline={"checkout": 200, "payment": 40}, known={"email"})
    silent = {b["service"]: b for b in breaches if b["signal"] == "silent"}
    assert set(silent) == {"payment", "email"}  # no data at all / below MIN_RPS
    assert silent["payment"]["value"] == 0.0 and silent["payment"]["severity"] == 10.0
    assert silent["payment"]["promql"] == detector.QUERIES["rps"]


def test_excluded_services_never_silent(monkeypatch):
    monkeypatch.setattr(detector, "snapshot", fake_snapshot({}))
    breaches, _ = detector.poll(baseline={}, known={"load-generator"})
    assert breaches == []


def test_silent_needs_consecutive_polls_in_run_loop(monkeypatch, tmp_path):
    """run(): a silent breach fires only after CONSECUTIVE polls, like any other breach."""
    monkeypatch.setattr(detector, "BASELINE_FILE", tmp_path / "baseline.json")
    (tmp_path / "baseline.json").write_text(json.dumps({"p95_ms": {"payment": 40}}))
    monkeypatch.setattr(detector, "snapshot", fake_snapshot({}))
    monkeypatch.setattr(detector, "POLL_SECS", 0)
    fired, polls = [], []

    def fake_sleep(_):
        polls.append(1)
        if len(polls) >= 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(detector.time, "sleep", fake_sleep)
    monkeypatch.setattr(detector, "start_investigation", fired.append)
    with pytest.raises(KeyboardInterrupt):
        detector.run()
    assert len(fired) == 1 and fired[0]["alerting_services"] == ["payment"]
    assert fired[0]["trigger"][0]["signal"] == "silent"


def test_query_drops_nan_and_inf(monkeypatch):
    body = {"status": "success", "data": {"result": [
        {"metric": {"service_name": "a"}, "value": [0, "NaN"]},
        {"metric": {"service_name": "b"}, "value": [0, "+Inf"]},
        {"metric": {"service_name": "c"}, "value": [0, "-Inf"]},
        {"metric": {"service_name": "d"}, "value": [0, "1.5"]},
    ]}}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return body

    monkeypatch.setattr(detector.requests, "get", lambda *a, **k: Resp())
    assert detector.query("x") == {"d": 1.5}


def test_files_live_next_to_detector():
    here = Path(detector.__file__).resolve().parent
    assert detector.BASELINE_FILE.parent == here and detector.INCIDENTS_FILE.parent == here
    assert math.isfinite(detector.MIN_RPS)
