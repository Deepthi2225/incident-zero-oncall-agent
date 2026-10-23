"""Jaeger v3 (OTLP JSON) adapter, on a trimmed real response from the demo's Jaeger 2.21."""
import json

from oncall.sources.jaeger_v3 import has_error, otlp_to_jaeger
from oncall.tools.traces import analyze_trace
from tests.conftest import FIXTURES

BODY = json.loads((FIXTURES / "jaeger_v3" / "checkout_trace.json").read_text())


def test_converts_one_trace_and_drops_flag_data():
    traces = otlp_to_jaeger(BODY["result"])
    assert len(traces) == 1
    t = traces[0]
    services = {p["serviceName"] for p in t["processes"].values()}
    assert "flagd" not in services and {"checkout", "payment", "frontend"} <= services
    text = json.dumps(t)
    assert "feature_flag" not in text and "SYNTHETIC flag eval" not in text


def test_kinds_status_and_parents_map_to_v1_shape():
    t = otlp_to_jaeger(BODY["result"])[0]
    assert has_error(t)
    charge = next(s for s in t["spans"] if s["operationName"] == "oteldemo.PaymentService/Charge"
                  and {"key": "span.kind", "value": "client"} in s["tags"])
    tags = {x["key"]: x["value"] for x in charge["tags"]}
    assert tags["error"] is True and tags["otel.status_code"] == "ERROR"
    assert "name resolver" in tags["otel.status_description"]
    assert charge["references"][0]["refType"] == "CHILD_OF" and charge["duration"] >= 0
    roots = [s for s in t["spans"] if not s["references"]]
    assert len(roots) >= 1


def test_trace_tool_finds_origin_in_converted_trace():
    a = analyze_trace(otlp_to_jaeger(BODY["result"])[0])
    origin = a["origins"][0]
    assert origin["service"] == "checkout" and origin["op"] == "oteldemo.PaymentService/Charge"
    assert "checkout" in a["chain"]
    assert any(h["from"] == "checkout" and h["to"] == "payment" for h in a["hops"])


def test_empty_result():
    assert otlp_to_jaeger({}) == [] and otlp_to_jaeger(None) == []
