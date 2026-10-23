import json

from oncall import config
from oncall.evidence import registry
from oncall.sources.bundle import BundleSource, RecordingSource
from oncall.tools.catalog import dispatch
from oncall.tools.logs import classify, search_logs
from oncall.tools.deploys import get_deploy_history
from oncall.tools.metrics import get_service_health, save_baseline
from oncall.tools.traces import analyze_trace, find_error_traces


def span(sid, parent, proc, op, kind, dur_us, error=False, msg=None, extra=None):
    tags = [{"key": "span.kind", "value": kind}]
    if error:
        tags += [{"key": "error", "value": True}, {"key": "otel.status_code", "value": "ERROR"}]
    if msg:
        tags.append({"key": "otel.status_description", "value": msg})
    for k, v in (extra or {}).items():
        tags.append({"key": k, "value": v})
    refs = [{"refType": "CHILD_OF", "spanID": parent}] if parent else []
    return {"spanID": sid, "operationName": op, "processID": proc, "references": refs,
            "startTime": 0, "duration": dur_us, "tags": tags, "logs": []}


PROCS = {"p1": {"serviceName": "frontend"}, "p2": {"serviceName": "checkout"},
         "p3": {"serviceName": "payment"}, "p4": {"serviceName": "product-catalog"}}


def propagated_payment_failure(tid, product="OLJCESPC7Z"):
    return {"traceID": tid, "processes": PROCS, "spans": [
        span("a", None, "p1", "POST /api/checkout", "server", 900_000, True, "500"),
        span("b", "a", "p1", "PlaceOrder", "client", 850_000, True, "upstream failed"),
        span("c", "b", "p2", "PlaceOrder", "server", 800_000, True, "failed to charge card"),
        span("d", "c", "p2", "Charge", "client", 700_000, True, "rpc error"),
        span("e", "d", "p3", "Charge", "server", 100_000, True, "Invalid token",
             {"app.product.id": product}),
    ]}


def test_deepest_failing_span_is_origin_and_hop_gap():
    a = analyze_trace(propagated_payment_failure("t1"))
    assert a["origins"][0]["service"] == "payment"
    assert a["chain"] == ["frontend", "checkout", "payment"]
    hop = next(h for h in a["hops"] if h["to"] == "payment")
    assert hop["client_ms"] == 700 and hop["server_ms"] == 100


def test_unreachable_callee_detected():
    trace = {"traceID": "t9", "processes": PROCS, "spans": [
        span("a", None, "p2", "PlaceOrder", "server", 5_000_000, True, "failed"),
        span("b", "a", "p2", "Charge", "client", 5_000_000, True, "connection refused",
             {"rpc.service": "oteldemo.PaymentService"}),
    ]}
    o = analyze_trace(trace)["origins"][0]
    assert o["service"] == "checkout" and o["no_server_span"]


def test_find_error_traces_groups_and_registers(mem):
    mem.traces = [propagated_payment_failure(f"t{i}") for i in range(4)]
    out = find_error_traces("frontend")
    assert out["traces_examined"] == 4
    top = out["origins"][0]
    assert top["origin_service"] == "payment" and top["count"] == 4
    assert top["evidence"] in registry
    cluster = out["attribute_clusters"][0]
    assert cluster["attribute"] == "app.product.id" and cluster["share"] == 1.0
    edge = next(e for e in out["edges"] if e["edge"] == "checkout->payment")
    assert edge["avg_gap_ms"] == 600 and "evidence" in edge


def test_no_traces_is_registered_as_evidence(mem):
    out = find_error_traces("cart")
    assert out["traces_examined"] == 0 and registry.get(out["evidence"])


def _series(svc, value):
    return {"metric": {"service_name": svc}, "value": [0, str(value)]}


def _health_data(mem, err_cart):
    calls = "traces_span_metrics_calls_total"
    mem.names = [calls, "traces_span_metrics_duration_milliseconds_bucket"]
    mem.instant = {
        f'rate({calls}{{span_kind=~': [_series("cart", 10), _series("frontend", 20)],
        f'rate({calls}{{span_kind=~"SPAN_KIND_SERVER|SPAN_KIND_CONSUMER",status_code=': [
            _series("cart", err_cart)],
        "histogram_quantile": [_series("cart", 40), _series("frontend", 80)],
    }


def test_service_health_flags_against_baseline(mem):
    _health_data(mem, err_cart=0.0)
    save_baseline("5m")
    _health_data(mem, err_cart=4.0)
    out = get_service_health("5m")
    top = out["flagged"][0]
    assert top["service"] == "cart" and "errors" in top["flags"]
    assert top["error_rate"] == 0.4 and top["baseline"]["error_rate"] == 0.0
    assert "frontend" in out["healthy"]
    assert "40.0%" in registry.get(top["evidence"])["summary"]


def test_silent_service_flagged(mem):
    _health_data(mem, err_cart=0.0)
    save_baseline("5m")
    mem.instant = {k: [s for s in v if s["metric"]["service_name"] != "cart"]
                    for k, v in mem.instant.items()}
    out = get_service_health("5m")
    assert out["flagged"][0]["service"] == "cart" and "silent" in out["flagged"][0]["flags"]


def test_logs_grouping(mem):
    mem.logs["cart"] = "\n".join([
        "2026-10-23T07:00:01.1Z fail: Cart.CartService[0] Can't access cart storage. "
        "System.ApplicationException: Wasn't able to connect to redis 10.0.0.5:6379",
        "2026-10-23T07:00:03.1Z fail: Cart.CartService[0] Can't access cart storage. "
        "System.ApplicationException: Wasn't able to connect to redis 10.0.0.5:6379",
        "2026-10-23T07:00:04.1Z info: GetCart called with userId=abc",
        '{"level":"warn","msg":"slow call 1200ms"}',
    ])
    out = search_logs(["cart"], since="5m", level="warn")
    top = out["patterns"][0]
    assert top["service"] == "cart" and top["count"] == 2 and top["level"] == "error"
    assert top["first_seen"].startswith("2026-10-23T07:00:01")
    assert out["services"]["cart"]["matching_lines"] == 3
    assert classify('{"level": 50, "msg": "boom"}') == ("error", "boom")


def test_container_health_reports_oom_and_stopped(mem):
    def inspect(name, status="running", oom=False, restarts=0, health="healthy", mem=20 * 2 ** 20):
        return {"Name": "/" + name, "RestartCount": restarts, "Created": "2026-01-01T00:00:00Z",
                "Config": {"Image": "demo"},
                "State": {"Status": status, "Running": status == "running", "OOMKilled": oom,
                          "ExitCode": 137 if oom else 0, "StartedAt": "2026-01-01T00:00:00Z",
                          "Health": {"Status": health}},
                "HostConfig": {"Memory": mem, "NanoCpus": 0}}
    mem.inspect = [inspect("checkout", "exited", oom=True, restarts=3), inspect("cart")]
    mem.stats = [{"Name": "cart", "CPUPerc": "1.5%", "MemUsage": "19.5MiB / 20MiB", "MemPerc": "97.5%"}]
    out = get_deploy_history()
    rows = out["changed_or_unhealthy"]
    assert rows[0]["name"] == "checkout"
    assert "OOMKilled" in rows[0]["problems"]
    assert any("memory 97.5%" in p for p in rows[1]["problems"])


def test_dispatch_catches_errors(mem):
    result, is_error = dispatch("find_error_traces", {"service": "x", "lookback": "soon"})
    assert is_error and "ValueError" in result["error"]
    result, is_error = dispatch("get_metric", {"promql": "up{job=\"flagd\"}"})
    assert is_error and "HARD RULE 2" in result["error"]


def test_bundle_record_and_replay(mem, tmp_path):
    mem.traces = [propagated_payment_failure("t1")]
    rec = RecordingSource(mem, tmp_path / "bundle")
    live_answer = rec.jaeger_traces("frontend", 900, 20, error_only=True)
    replay = BundleSource(tmp_path / "bundle")
    assert replay.jaeger_traces("frontend", 900, 20, error_only=True) == live_answer
    assert replay.jaeger_traces("cart", 900, 20, error_only=True) == []
    assert json.loads((tmp_path / "bundle" / "meta.json").read_text())["captured_at"]
    assert config.SOURCE in ("live", "bundle")
