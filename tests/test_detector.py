"""detector.py: server-span signals, relative silence, top-bucket-aware calibration, one detector."""
import json
import math
from pathlib import Path

import pytest

import detector
from oncall import config


def test_error_rate_and_p95_count_only_server_spans():
    numerator, denominator = detector.QUERIES["error_rate"].split(" / ")
    assert 'span_kind="SPAN_KIND_SERVER"' in numerator and 'status_code="STATUS_CODE_ERROR"' in numerator
    assert 'span_kind="SPAN_KIND_SERVER"' in denominator
    assert 'span_kind="SPAN_KIND_SERVER"' in detector.QUERIES["p95_ms"]  # no long-lived client spans
    assert "span_kind" not in detector.QUERIES["rps"]  # rps stays on all spans


def fake_snapshot(rps, err=None, p95=None):
    return lambda: {"rps": rps, "error_rate": err or {}, "p95_ms": p95 or {}}


BASE = {
    "checkout": {"p95_ms": 1580.0, "rps": 0.04},
    "payment": {"p95_ms": 2.0, "rps": 0.04},
    "email": {"p95_ms": 7.6, "rps": 0.04},
    "telemetry-docs": {"p95_ms": 2.0, "rps": 0.003},   # idle: below the absolute floor
}


# --- fix 1: silence relative to each service's own baseline ------------------------------

def test_silent_relative_to_own_baseline(monkeypatch):
    # checkout at 50% of baseline is fine; email at 10% and payment gone are silent.
    monkeypatch.setattr(detector, "snapshot", fake_snapshot({"checkout": 0.02, "email": 0.004}))
    breaches, _ = detector.poll(BASE)
    silent = {b["service"]: b for b in breaches if b["signal"] == "silent"}
    assert set(silent) == {"payment", "email"}
    assert silent["payment"]["value"] == 0.0 and silent["payment"]["threshold"] == pytest.approx(0.008)
    assert silent["email"]["severity"] == 10.0


def test_low_traffic_services_are_not_silent_at_normal_rates(monkeypatch):
    """The old absolute 0.05 rps floor flagged every low-traffic service; the relative rule doesn't."""
    monkeypatch.setattr(detector, "snapshot", fake_snapshot({"checkout": 0.04, "payment": 0.035, "email": 0.04}))
    breaches, _ = detector.poll(BASE)
    assert [b for b in breaches if b["signal"] == "silent"] == []


def test_idle_and_excluded_services_never_silent(monkeypatch):
    monkeypatch.setattr(detector, "snapshot", fake_snapshot({}))
    breaches, _ = detector.poll({"telemetry-docs": BASE["telemetry-docs"]}, peaks={"load-generator": 5.0})
    assert breaches == []


def test_peak_rps_is_the_reference_without_a_baseline(monkeypatch):
    monkeypatch.setattr(detector, "snapshot", fake_snapshot({"quote": 0.01}))
    breaches, _ = detector.poll({}, peaks={"quote": 0.5})
    assert [(b["service"], b["threshold"]) for b in breaches] == [("quote", 0.1)]


def test_silent_fires_after_consecutive_polls_and_hands_off(monkeypatch, tmp_path):
    monkeypatch.setattr(detector, "BASELINE_FILE", tmp_path / "baseline.json")
    (tmp_path / "baseline.json").write_text(json.dumps({"services": {"payment": {"p95_ms": 2.0, "rps": 0.04}}}))
    monkeypatch.setattr(detector, "snapshot", fake_snapshot({}))
    monkeypatch.setattr(detector.time, "sleep", lambda s: None)
    polls, fired = [], []
    real_poll = detector.poll

    def counting_poll(*a, **k):
        polls.append(1)
        return real_poll(*a, **k)

    monkeypatch.setattr(detector, "poll", counting_poll)
    detector.run(on_incident=fired.append, once=True)
    assert len(polls) == detector.CONSECUTIVE and len(fired) == 1
    assert fired[0]["alerting_services"] == ["payment"] and fired[0]["trigger"][0]["signal"] == "silent"


# --- fix 3: calibration ignores the histogram's top bucket ---------------------------------

def test_learn_baseline_ignores_top_bucket_and_marks_unknown():
    samples = {
        "checkout": {"p95": [1500.0, 15000.0, 1580.0], "rps": [0.04, 0.05, 0.03]},
        "payment": {"p95": [15000.0, 15000.0], "rps": [0.04, 0.04]},
        "cart": {"p95": [3.9, float("nan")], "rps": [3.0]},
    }
    b = detector.learn_baseline(samples, top_ms=15000.0)
    assert b["checkout"] == {"p95_ms": 1580.0, "rps": 0.04}  # max of non-saturated, median rps
    assert b["payment"]["p95_ms"] is None and b["payment"]["rps"] == 0.04
    assert b["cart"]["p95_ms"] == 3.9


def test_unknown_latency_baseline_falls_back_to_median_rule():
    base = {"a": {"p95_ms": 100.0}, "b": {"p95_ms": 400.0}, "c": {"p95_ms": 1000.0},
            "payment": {"p95_ms": None, "rps": 0.04}}
    assert detector.p95_limit("payment", base) == detector.LATENCY_MULT * 400.0  # median of known
    assert detector.p95_limit("a", base) == detector.P95_FLOOR_MS               # 2x100 < floor
    assert detector.p95_limit("c", base) == 2000.0
    assert detector.p95_limit("new", base) is None
    assert detector.p95_limit("payment", {"payment": {"p95_ms": None}}) == detector.P95_FLOOR_MS


def test_unknown_baseline_still_alerts(monkeypatch):
    base = {"x": {"p95_ms": 300.0, "rps": 1.0}, "payment": {"p95_ms": None, "rps": 1.0}}
    monkeypatch.setattr(detector, "snapshot", fake_snapshot({"x": 1.0, "payment": 1.0},
                                                            p95={"x": 100.0, "payment": 15000.0}))
    breaches, _ = detector.poll(base)
    assert [(b["service"], b["signal"], b["threshold"]) for b in breaches] == [("payment", "p95_ms", 600.0)]


def test_top_bucket_discovered_from_prometheus(monkeypatch):
    class Resp:
        def json(self):
            return {"data": ["2", "800", "15000", "+Inf", "5000"]}
    monkeypatch.setattr(detector.requests, "get", lambda *a, **k: Resp())
    assert detector.top_bucket_ms() == 15000.0


def test_old_baseline_format_still_loads(monkeypatch, tmp_path):
    monkeypatch.setattr(detector, "BASELINE_FILE", tmp_path / "baseline.json")
    (tmp_path / "baseline.json").write_text(json.dumps({"p95_ms": {"cart": 3.9}}))
    assert detector.load_baseline() == {"cart": {"p95_ms": 3.9, "rps": None}}
    assert detector.silent_threshold("cart", detector.load_baseline(), None) == detector.MIN_RPS


# --- fix 2: one detector -----------------------------------------------------------------

def test_detect_once_returns_incident_or_none(monkeypatch, tmp_path):
    monkeypatch.setattr(detector, "BASELINE_FILE", tmp_path / "baseline.json")
    (tmp_path / "baseline.json").write_text(json.dumps({"services": BASE}))
    monkeypatch.setattr(detector, "snapshot", fake_snapshot({"checkout": 0.04, "payment": 0.04, "email": 0.04}))
    assert detector.detect_once() is None
    monkeypatch.setattr(detector, "snapshot", fake_snapshot({"checkout": 0.04, "email": 0.04},
                                                            err={"checkout": 1.0}))
    inc = detector.detect_once()
    assert inc["alerting_services"] == ["checkout", "payment"]
    assert {(t["service"], t["signal"]) for t in inc["trigger"]} == {("checkout", "error_rate"), ("payment", "silent")}


def test_one_detector_watcher_removed():
    assert not (config.ROOT / "oncall" / "watcher.py").exists()
    demo = (config.ROOT / "scripts" / "demo.py").read_text(encoding="utf-8")
    main = (config.ROOT / "oncall" / "__main__.py").read_text(encoding="utf-8")
    assert "detector.detect_once()" in demo and "detector.run(on_incident=run_agent" in main
    assert "watcher" not in demo and "oncall.watcher" not in main


# --- unchanged behaviour -------------------------------------------------------------------

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
