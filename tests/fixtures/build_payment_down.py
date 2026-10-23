"""Generate the synthetic `payment_down` fixtures (payment container stopped with `docker stop`).

The JSON is shaped exactly like the real responses (Prometheus HTTP API, Jaeger query API,
`docker inspect`, `docker stats --format '{{json .}}'`, `docker logs --timestamps`), but the
numbers and messages are invented for tests. Re-run after changing: python tests/fixtures/build_payment_down.py
"""
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parent / "payment_down"
NOW = 1792740000                       # 2026-10-23T07:20:00Z
STOPPED = NOW - 100                    # payment stopped 100 s before the snapshot
BOOT = NOW - 3000                      # stack started 50 min earlier


def iso(t, nanos=True):
    s = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    frac = f"{t - int(t):.9f}"[1:] if nanos else ""
    return s + (frac if nanos else "") + "Z"


def write(rel, data):
    path = OUT / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    text = data if isinstance(data, str) else json.dumps(data, indent=1)
    path.write_text(text, encoding="utf-8", newline="\n")


# --- Prometheus ---------------------------------------------------------------
CALLS = "traces_span_metrics_calls_total"
DUR = "traces_span_metrics_duration_milliseconds_bucket"


def vector(values):
    return {"status": "success", "data": {"resultType": "vector", "result": [
        {"metric": {"service_name": svc}, "value": [NOW, str(v)]} for svc, v in values.items()]}}


def matrix(series, step=30, points=30):
    result = []
    for labels, fn in series:
        values = [[NOW - (points - i) * step, str(fn(i))] for i in range(points)]
        result.append({"metric": labels, "values": [v for v in values if v[1] != "None"]})
    return {"status": "success", "data": {"resultType": "matrix", "result": result}}


SERVER_RPS = {"frontend-proxy": 9.8, "frontend": 9.1, "cart": 2.1, "checkout": 0.42,
              "currency": 3.0, "product-catalog": 6.2, "recommendation": 1.5, "ad": 1.8,
              "shipping": 0.6, "quote": 0.4, "image-provider": 4.4}
SERVER_ERR = {"frontend-proxy": 0.4, "frontend": 0.4, "checkout": 0.4}
CLIENT_RPS = {"frontend-proxy": 9.8, "frontend": 15.2, "checkout": 2.5, "recommendation": 1.5,
              "shipping": 0.4}
CLIENT_ERR = {"frontend-proxy": 0.4, "frontend": 0.4, "checkout": 0.4}
P95 = {"frontend-proxy": 190, "frontend": 180, "cart": 9, "checkout": 45, "currency": 2,
       "product-catalog": 6, "recommendation": 30, "ad": 12, "shipping": 8, "quote": 15,
       "image-provider": 4}

BASELINE = {
    "window_s": 300, "captured_at": BOOT + 900, "queries": {},
    "services": {svc: {"rps": rps, "error_rate": 0.0, "client_error_rate": 0.0, "p95_ms": p}
                 for svc, rps, p in [
                     ("frontend-proxy", 9.6, 185), ("frontend", 9.0, 170), ("cart", 2.0, 9),
                     ("checkout", 0.41, 260), ("currency", 3.1, 2), ("product-catalog", 6.0, 6),
                     ("recommendation", 1.5, 28), ("ad", 1.8, 12), ("shipping", 0.8, 8),
                     ("quote", 0.4, 15), ("image-provider", 4.2, 4), ("payment", 0.4, 35),
                     ("email", 0.38, 40)]},
}

LABEL_NAMES = sorted([
    CALLS, DUR, "traces_span_metrics_duration_milliseconds_count",
    "traces_span_metrics_duration_milliseconds_sum",
    "container_cpu_utilization_ratio", "container_cpu_usage_total_nanoseconds_total",
    "container_memory_usage_total_bytes", "container_memory_usage_limit_bytes",
    "container_memory_percent_ratio", "container_memory_rss_bytes", "container_uptime_seconds",
    "redis_clients_connected", "redis_memory_used_bytes", "redis_uptime_seconds_total",
    "postgresql_backends", "postgresql_commits_total", "postgresql_deadlocks_total",
    "system_cpu_utilization_ratio", "system_memory_utilization_ratio",
    "httpcheck_status", "httpcheck_duration_milliseconds",
    "jvm_memory_used_bytes", "jvm_gc_duration_seconds_bucket", "jvm_cpu_recent_utilization_ratio",
    "process_runtime_dotnet_gc_collections_count_total", "dotnet_gc_heap_total_allocated_bytes_total",
    "nodejs_eventloop_delay_p99_seconds", "v8js_memory_heap_used_bytes",
    "process_cpu_utilization_ratio", "process_memory_usage_bytes", "app_ads_ad_requests_total",
    "target_info",
])


def build_prometheus():
    write("prometheus/label_names.json", {"status": "success", "data": LABEL_NAMES})
    write("prometheus/server_calls.json", vector(SERVER_RPS))
    write("prometheus/server_errors.json", vector(SERVER_ERR))
    write("prometheus/client_calls.json", vector(CLIENT_RPS))
    write("prometheus/client_errors.json", vector(CLIENT_ERR))
    write("prometheus/p95.json", vector(P95))
    stop_idx = 30 - 100 // 30  # last samples after the stop are missing for payment
    write("prometheus/range_container_memory.json", matrix([
        ({"container_name": "payment"}, lambda i: None if i >= stop_idx else 0.52 + 0.001 * i),
        ({"container_name": "checkout"}, lambda i: 0.41),
        ({"container_name": "cart"}, lambda i: 0.62),
    ]))
    write("prometheus/manifest.json", [
        {"type": "query", "match": ["histogram_quantile"], "file": "p95.json"},
        {"type": "query", "match": ["SPAN_KIND_CLIENT", "STATUS_CODE_ERROR"], "file": "client_errors.json"},
        {"type": "query", "match": ["SPAN_KIND_CLIENT"], "file": "client_calls.json"},
        {"type": "query", "match": ["STATUS_CODE_ERROR"], "file": "server_errors.json"},
        {"type": "query", "match": [CALLS], "file": "server_calls.json"},
        {"type": "range", "match": ["container_memory"], "file": "range_container_memory.json"},
    ])
    write("baseline.json", BASELINE)


# --- Jaeger -------------------------------------------------------------------
PROCS = {
    "p1": "frontend-proxy", "p2": "frontend", "p3": "checkout", "p4": "cart",
    "p5": "product-catalog", "p6": "currency", "p7": "shipping", "p8": "payment",
}


def tag(key, value):
    kind = "bool" if isinstance(value, bool) else "int64" if isinstance(value, int) else "string"
    return {"key": key, "type": kind, "value": value}


def span(trace_id, sid, parent, proc, op, kind, start, dur, error=None, extra=()):
    tags = [tag("span.kind", kind), tag("otel.scope.name", "demo")]
    tags += [tag(k, v) for k, v in extra]
    if error:
        tags += [tag("error", True), tag("otel.status_code", "ERROR"),
                 tag("otel.status_description", error)]
    return {
        "traceID": trace_id, "spanID": sid, "operationName": op,
        "references": [{"refType": "CHILD_OF", "traceID": trace_id, "spanID": parent}] if parent else [],
        "startTime": start, "duration": dur, "tags": tags, "logs": [], "processID": proc,
        "warnings": None, "flags": 1,
    }


def processes():
    return {pid: {"serviceName": svc, "tags": [tag("service.namespace", "opentelemetry-demo"),
                                               tag("telemetry.sdk.language", "go")]}
            for pid, svc in PROCS.items()}


CHARGE_ERR = ("failed to charge card: could not charge the card: rpc error: code = Unavailable "
              "desc = name resolver error: produced zero addresses")


def failing_checkout_trace(n, t_us):
    tid = f"7b2e4c{n:026x}"
    s = lambda k: f"{n:02x}{k:014x}"  # noqa: E731
    return {"traceID": tid, "processes": processes(), "warnings": None, "spans": [
        span(tid, s(1), None, "p1", "ingress", "server", t_us, 61000,
             "500", [("http.response.status_code", 500), ("upstream_cluster", "frontend")]),
        span(tid, s(2), s(1), "p2", "POST /api/checkout", "server", t_us + 500, 59000,
             "13 INTERNAL: " + CHARGE_ERR, [("http.response.status_code", 500)]),
        span(tid, s(3), s(2), "p2", "oteldemo.CheckoutService/PlaceOrder", "client", t_us + 900,
             56000, "13 INTERNAL: " + CHARGE_ERR,
             [("rpc.system", "grpc"), ("rpc.service", "oteldemo.CheckoutService"),
              ("rpc.grpc.status_code", 13)]),
        span(tid, s(4), s(3), "p3", "oteldemo.CheckoutService/PlaceOrder", "server", t_us + 1500,
             52000, CHARGE_ERR, [("rpc.system", "grpc"), ("rpc.grpc.status_code", 13),
                                 ("app.user.id", f"user-{n}"), ("app.user.currency", "USD")]),
        span(tid, s(5), s(4), "p3", "oteldemo.CartService/GetCart", "client", t_us + 2000, 9000,
             None, [("rpc.service", "oteldemo.CartService")]),
        span(tid, s(6), s(5), "p4", "oteldemo.CartService/GetCart", "server", t_us + 2400, 7000),
        span(tid, s(7), s(4), "p3", "oteldemo.ProductCatalogService/GetProduct", "client",
             t_us + 12000, 5000, None, [("rpc.service", "oteldemo.ProductCatalogService")]),
        span(tid, s(8), s(7), "p5", "oteldemo.ProductCatalogService/GetProduct", "server",
             t_us + 12300, 4100, None, [("app.product.id", ["OLJCESPC7Z", "66VCHSJNUP", "1YMWWN1N4O"][n % 3])]),
        span(tid, s(9), s(4), "p3", "oteldemo.CurrencyService/Convert", "client", t_us + 18000,
             2500, None, [("rpc.service", "oteldemo.CurrencyService")]),
        span(tid, s(10), s(9), "p6", "Convert", "server", t_us + 18200, 1800),
        span(tid, s(11), s(4), "p3", "POST", "client", t_us + 22000, 9500, None,
             [("url.full", "http://shipping:50050/get-quote")]),
        span(tid, s(12), s(11), "p7", "POST /get-quote", "server", t_us + 22400, 8100),
        span(tid, s(13), s(4), "p3", "oteldemo.PaymentService/Charge", "client", t_us + 33000,
             1200, "name resolver error: produced zero addresses",
             [("rpc.system", "grpc"), ("rpc.service", "oteldemo.PaymentService"),
              ("rpc.method", "Charge"), ("rpc.grpc.status_code", 14), ("server.address", "payment")]),
    ]}


def healthy_payment_trace(n, t_us):
    tid = f"3c19aa{n:026x}"
    s = lambda k: f"{n + 64:02x}{k:014x}"  # noqa: E731
    return {"traceID": tid, "processes": processes(), "warnings": None, "spans": [
        span(tid, s(1), None, "p3", "oteldemo.CheckoutService/PlaceOrder", "server", t_us, 240000),
        span(tid, s(2), s(1), "p3", "oteldemo.PaymentService/Charge", "client", t_us + 150000,
             38000, None, [("rpc.service", "oteldemo.PaymentService"), ("server.address", "payment")]),
        span(tid, s(3), s(2), "p8", "oteldemo.PaymentService/Charge", "server", t_us + 150600,
             34000, None, [("app.payment.card_type", "visa"), ("app.payment.charged", True)]),
    ]}


def build_jaeger():
    write("jaeger/services.json", {"data": sorted(set(PROCS.values()) | {"recommendation", "ad", "quote", "email"}),
                                   "total": 12, "limit": 0, "offset": 0, "errors": None})
    failing = [failing_checkout_trace(i, (NOW - 90 + 15 * i) * 1_000_000) for i in range(5)]
    healthy = [healthy_payment_trace(i, (STOPPED - 400 + 60 * i) * 1_000_000) for i in range(3)]
    envelope = lambda traces: {"data": traces, "total": len(traces), "limit": 0, "offset": 0, "errors": None}  # noqa: E731
    write("jaeger/traces_frontend.json", envelope(failing))
    write("jaeger/traces_frontend-proxy.json", envelope(failing))
    write("jaeger/traces_checkout.json", envelope(failing + healthy))
    write("jaeger/traces_payment.json", envelope(healthy))


# --- Docker -------------------------------------------------------------------
MEM = {"frontend-proxy": 90, "frontend": 250, "checkout": 20, "payment": 140, "cart": 160,
       "currency": 20, "product-catalog": 20, "shipping": 20, "ad": 300, "email": 100,
       "recommendation": 500, "quote": 40, "load-generator": 1500, "valkey-cart": 20,
       "otel-collector": 400, "jaeger": 1200, "prometheus": 300, "flagd": 75}
HEALTHCHECK = {"frontend", "cart", "checkout", "product-catalog", "valkey-cart", "jaeger",
               "prometheus", "otel-collector"}


def inspect(name):
    running = name != "payment"
    state = {
        "Status": "running" if running else "exited", "Running": running, "Paused": False,
        "Restarting": False, "OOMKilled": False, "Dead": False, "Pid": 4100 if running else 0,
        "ExitCode": 0 if running else 143, "Error": "",
        "StartedAt": iso(BOOT + 0.123456789), "FinishedAt": iso(STOPPED + 0.5) if not running else "0001-01-01T00:00:00Z",
    }
    if name in HEALTHCHECK:
        state["Health"] = {"Status": "healthy", "FailingStreak": 0, "Log": []}
    return {
        "Id": hashlib.sha256(name.encode()).hexdigest(),
        "Created": iso(BOOT - 5 + 0.25), "Path": "/app/start", "Args": [], "State": state,
        "Name": f"/{name}", "RestartCount": 0,
        "HostConfig": {"Memory": MEM.get(name, 0) * 1024 * 1024, "NanoCpus": 0,
                       "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0}},
        "Config": {"Hostname": name, "Image": f"ghcr.io/open-telemetry/demo:latest-{name}",
                   "Labels": {"com.docker.compose.project": "opentelemetry-demo",
                              "com.docker.compose.service": name}},
    }


def build_docker():
    names = list(MEM)
    write("docker/inspect.json", [inspect(n) for n in names])
    usage = {"checkout": 8.2, "cart": 99.0, "frontend": 140.0, "valkey-cart": 4.1, "ad": 210.0}
    lines = []
    for n in names:
        if n == "payment":
            continue  # docker stats only lists running containers
        used = usage.get(n, MEM[n] * 0.3)
        lines.append(json.dumps({
            "BlockIO": "0B / 0B", "CPUPerc": "1.20%", "Container": n, "ID": n,
            "MemPerc": f"{used / MEM[n] * 100:.2f}%", "MemUsage": f"{used}MiB / {MEM[n]}MiB",
            "Name": n, "NetIO": "1.2MB / 900kB", "PIDs": "12"}))
    write("docker/stats.jsonl", "\n".join(lines) + "\n")

    checkout, frontend, payment, cart = [], [], [], []
    for i in range(6):
        t = NOW - 95 + 15 * i
        checkout.append(f"{iso(t)} " + json.dumps({"level": "info", "msg": f"[PlaceOrder] user_id=user-{i} user_currency=USD", "time": iso(t, False)}))
        checkout.append(f"{iso(t + 0.05)} " + json.dumps({"level": "error", "msg": CHARGE_ERR, "time": iso(t + 0.05, False)}))
        frontend.append(f"{iso(t + 0.06)} Error: 13 INTERNAL: {CHARGE_ERR}")
        frontend.append(f"{iso(t + 0.06)}     at callErrorFromStatus (/app/node_modules/@grpc/grpc-js/build/src/call.js:31:19)")
    for i in range(8):
        t = STOPPED - 300 + 35 * i
        payment.append(f"{iso(t)} " + json.dumps({"level": 30, "time": t * 1000, "pid": 1, "hostname": "payment", "msg": "Charge request received.", "request": {"amount": {"currencyCode": "USD", "units": 120 + i}}}))
        payment.append(f"{iso(t + 0.02)} " + json.dumps({"level": 30, "time": t * 1000 + 20, "pid": 1, "hostname": "payment", "msg": "Transaction complete.", "transactionId": f"{i:08x}-0000-4000-8000-000000000000", "cardType": "visa"}))
    for i in range(10):
        t = NOW - 200 + 20 * i
        cart.append(f"{iso(t)} info: cart.cartstore.ValkeyCartStore[0] GetCartAsync called with userId=user-{i}")
    write("docker/logs/checkout.log", "\n".join(checkout) + "\n")
    write("docker/logs/frontend.log", "\n".join(frontend) + "\n")
    write("docker/logs/payment.log", "\n".join(payment) + "\n")
    write("docker/logs/cart.log", "\n".join(cart) + "\n")
    write("docker/logs/email.log", "")


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    write("meta.json", {"now": NOW, "scenario": "payment container stopped with docker stop",
                        "synthetic": True})
    write("alert.json", {"source": "watcher", "fired_at": iso(NOW, False), "service": "checkout",
                         "signal": "error_rate", "value": 0.952, "threshold": 0.05, "window_s": 120,
                         "other_breaches": [{"service": "frontend", "signal": "error_rate",
                                             "value": 0.044, "threshold": 0.05}]})
    build_prometheus()
    build_jaeger()
    build_docker()
    print(f"wrote fixtures to {OUT}")


if __name__ == "__main__":
    main()
