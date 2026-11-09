"""Generate three synthetic APPLICATION-fault fixtures (containers healthy, the code failing), in the
same format as payment_down:

    payment_app_error   payment's own Charge handler returns errors; checkout fails with it
    product_one_item    product-catalog fails only for one product id; everything else normal
    homepage_flood      request rate at the entry points several times the baseline; latency up

Shaped like the real responses (Prometheus HTTP API, Jaeger query API, docker inspect/stats/logs);
numbers and messages are invented. No feature-flag data anywhere (not even Jaeger's "flags" field).
Re-run after changing: python tests/fixtures/build_app_faults.py
"""
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
NOW = 1792740000                       # 2026-10-23T07:20:00Z
BOOT = NOW - 3000
CALLS = "traces_span_metrics_calls_total"
DUR = "traces_span_metrics_duration_milliseconds_bucket"

BASE = {"frontend-proxy": (9.6, 185), "frontend": (9.0, 170), "cart": (2.0, 9), "checkout": (0.41, 260),
        "currency": (3.1, 2), "product-catalog": (6.0, 6), "recommendation": (1.5, 28), "ad": (1.8, 12),
        "shipping": (0.8, 8), "quote": (0.4, 15), "image-provider": (4.2, 4), "payment": (0.4, 35),
        "email": (0.38, 40)}
MEM = {"frontend-proxy": 90, "frontend": 250, "checkout": 20, "payment": 140, "cart": 160, "currency": 20,
       "product-catalog": 20, "shipping": 20, "ad": 300, "email": 100, "recommendation": 500, "quote": 40,
       "image-provider": 120, "load-generator": 1500, "valkey-cart": 20, "astronomy-db": 80,
       "otel-collector": 400, "jaeger": 1200, "prometheus": 300}
HEALTHCHECK = {"frontend", "cart", "checkout", "product-catalog", "valkey-cart", "jaeger", "prometheus",
               "otel-collector"}
LABEL_NAMES = sorted([
    CALLS, DUR, "traces_span_metrics_duration_milliseconds_count", "traces_span_metrics_duration_milliseconds_sum",
    "container_cpu_utilization_ratio", "container_memory_usage_total_bytes", "container_memory_usage_limit_bytes",
    "container_memory_percent_ratio", "container_uptime_seconds", "process_cpu_utilization_ratio",
    "process_memory_usage_bytes", "jvm_memory_used_bytes", "jvm_gc_duration_seconds_bucket",
    "nodejs_eventloop_delay_p99_seconds", "httpcheck_status", "target_info",
])
PROCS = {"p1": "frontend-proxy", "p2": "frontend", "p3": "checkout", "p4": "cart", "p5": "product-catalog",
         "p6": "currency", "p7": "shipping", "p8": "payment"}


def iso(t, nanos=True):
    s = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return s + (f"{t - int(t):.9f}"[1:] if nanos else "") + "Z"


class Fixture:
    def __init__(self, name):
        self.out = HERE / name
        if self.out.exists():
            shutil.rmtree(self.out)

    def write(self, rel, data):
        path = self.out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data, indent=1), encoding="utf-8", newline="\n")


def vector(values):
    return {"status": "success", "data": {"resultType": "vector", "result": [
        {"metric": {"service_name": svc}, "value": [NOW, str(v)]} for svc, v in values.items()]}}


def matrix(series, step=30, points=30):
    return {"status": "success", "data": {"resultType": "matrix", "result": [
        {"metric": labels, "values": [[NOW - (points - i) * step, str(fn(i))] for i in range(points)]}
        for labels, fn in series]}}


def tag(key, value):
    kind = "bool" if isinstance(value, bool) else "int64" if isinstance(value, int) else "string"
    return {"key": key, "type": kind, "value": value}


def span(tid, sid, parent, proc, op, kind, start, dur, error=None, extra=()):
    tags = [tag("span.kind", kind), tag("otel.scope.name", "demo")] + [tag(k, v) for k, v in extra]
    if error:
        tags += [tag("error", True), tag("otel.status_code", "ERROR"), tag("otel.status_description", error)]
    return {"traceID": tid, "spanID": sid, "operationName": op,
            "references": [{"refType": "CHILD_OF", "traceID": tid, "spanID": parent}] if parent else [],
            "startTime": start, "duration": dur, "tags": tags, "logs": [], "processID": proc, "warnings": None}


def processes():
    return {pid: {"serviceName": svc, "tags": [tag("service.namespace", "opentelemetry-demo")]}
            for pid, svc in PROCS.items()}


def envelope(traces):
    return {"data": traces, "total": len(traces), "limit": 0, "offset": 0, "errors": None}


def inspect(name, restarts=0):
    state = {"Status": "running", "Running": True, "Paused": False, "Restarting": False, "OOMKilled": False,
             "Dead": False, "Pid": 4100, "ExitCode": 0, "Error": "", "StartedAt": iso(BOOT + 0.123456789),
             "FinishedAt": "0001-01-01T00:00:00Z"}
    if name in HEALTHCHECK:
        state["Health"] = {"Status": "healthy", "FailingStreak": 0, "Log": []}
    return {"Id": hashlib.sha256(name.encode()).hexdigest(), "Created": iso(BOOT - 5 + 0.25),
            "Path": "/app/start", "Args": [], "State": state, "Name": f"/{name}", "RestartCount": restarts,
            "HostConfig": {"Memory": MEM[name] * 1024 * 1024, "NanoCpus": 0,
                           "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0}},
            "Config": {"Hostname": name, "Image": f"ghcr.io/open-telemetry/demo:latest-{name}",
                       "Labels": {"com.docker.compose.project": "opentelemetry-demo",
                                  "com.docker.compose.service": name}}}


def common(fx, scenario, alert, server_rps, server_err, client_rps, client_err, p95, cpu=None, range_rps=None):
    fx.write("meta.json", {"now": NOW, "scenario": scenario, "synthetic": True})
    fx.write("alert.json", alert)
    fx.write("baseline.json", {"window_s": 300, "captured_at": BOOT + 900, "queries": {}, "services": {
        svc: {"rps": rps, "error_rate": 0.0, "client_error_rate": 0.0, "p95_ms": p} for svc, (rps, p) in BASE.items()}})
    fx.write("prometheus/label_names.json", {"status": "success", "data": LABEL_NAMES})
    fx.write("prometheus/server_calls.json", vector(server_rps))
    fx.write("prometheus/server_errors.json", vector(server_err))
    fx.write("prometheus/client_calls.json", vector(client_rps))
    fx.write("prometheus/client_errors.json", vector(client_err))
    fx.write("prometheus/p95.json", vector(p95))
    fx.write("prometheus/range_container_memory.json", matrix([
        ({"container_name": n}, lambda i, b=b: round(b + 0.0005 * (i % 3), 4))
        for n, b in (("payment", 0.42), ("product-catalog", 0.38), ("frontend", 0.56), ("checkout", 0.41))]))
    manifest = [
        {"type": "query", "match": ["histogram_quantile"], "file": "p95.json"},
        {"type": "query", "match": ["SPAN_KIND_CLIENT", "STATUS_CODE_ERROR"], "file": "client_errors.json"},
        {"type": "query", "match": ["SPAN_KIND_CLIENT"], "file": "client_calls.json"},
        {"type": "query", "match": ["STATUS_CODE_ERROR"], "file": "server_errors.json"},
        {"type": "query", "match": [CALLS], "file": "server_calls.json"},
        {"type": "range", "match": ["container_memory"], "file": "range_container_memory.json"},
    ]
    if range_rps:
        fx.write("prometheus/range_calls.json", matrix([({"service_name": s}, fn) for s, fn in range_rps.items()]))
        manifest.append({"type": "range", "match": [CALLS], "file": "range_calls.json"})
    fx.write("prometheus/manifest.json", manifest)
    fx.write("jaeger/services.json", envelope(sorted(set(PROCS.values()) | {"recommendation", "ad", "quote", "email"})))
    names = [n for n in MEM]
    fx.write("docker/inspect.json", [inspect(n) for n in names])
    cpu = cpu or {}
    fx.write("docker/stats.jsonl", "\n".join(json.dumps({
        "BlockIO": "0B / 0B", "CPUPerc": f"{cpu.get(n, 1.2):.2f}%", "Container": n, "ID": n,
        "MemPerc": f"{30:.2f}%", "MemUsage": f"{MEM[n] * 0.3:.1f}MiB / {MEM[n]}MiB", "Name": n,
        "NetIO": "1.2MB / 900kB", "PIDs": "12"}) for n in names) + "\n")


# --- a. payment's own code fails ------------------------------------------------------------------------
PAY_ERR = "Payment request failed. Invalid token. app.loyalty.level=gold"


def payment_app_error():
    fx = Fixture("payment_app_error")
    rps = {s: r for s, (r, _) in BASE.items()}
    rps.update(email=0.03)  # few orders complete, so few confirmation emails
    common(fx, "payment's Charge handler returns errors; container healthy",
           {"incident_id": "INC-APP-PAYMENT", "detected_at": iso(NOW, False), "alerting_services": ["checkout"],
            "trigger": [{"service": "checkout", "signal": "error_rate", "value": 0.93, "threshold": 0.05, "window": "3m"}],
            "note": "alerting service is a symptom, not necessarily the root cause"},
           server_rps=rps, server_err={"payment": 0.37, "checkout": 0.38, "frontend": 0.38, "frontend-proxy": 0.38},
           client_rps={"frontend-proxy": 9.8, "frontend": 15.2, "checkout": 2.5}, client_err={"checkout": 0.37, "frontend": 0.38},
           p95={s: p for s, (_, p) in BASE.items()})

    def trace(n, t, ok):
        tid = f"5a1b{n:028x}"
        s = lambda k: f"{n:02x}{k:014x}"  # noqa: E731
        err = None if ok else PAY_ERR
        up = None if ok else f"13 INTERNAL: failed to charge card: could not charge the card: rpc error: code = Unknown desc = {PAY_ERR}"
        return {"traceID": tid, "processes": processes(), "warnings": None, "spans": [
            span(tid, s(1), None, "p2", "POST /api/checkout", "server", t, 61000, up, [("http.response.status_code", 200 if ok else 500)]),
            span(tid, s(2), s(1), "p3", "oteldemo.CheckoutService/PlaceOrder", "server", t + 900, 58000, up,
                 [("rpc.system", "grpc"), ("app.user.id", f"user-{n}")]),
            span(tid, s(3), s(2), "p3", "oteldemo.PaymentService/Charge", "client", t + 30000, 9000, up and f"rpc error: code = Unknown desc = {PAY_ERR}",
                 [("rpc.service", "oteldemo.PaymentService"), ("server.address", "payment")]),
            span(tid, s(4), s(3), "p8", "oteldemo.PaymentService/Charge", "server", t + 30500, 7600, err,
                 [("rpc.system", "grpc"), ("app.payment.card_type", "visa"), ("app.payment.charged", ok)]),
        ]}

    failing = [trace(i, (NOW - 120 + 15 * i) * 1_000_000, ok=False) for i in range(6)]
    healthy = [trace(40 + i, (NOW - 900 + 60 * i) * 1_000_000, ok=True) for i in range(3)]
    for svc in ("frontend", "frontend-proxy", "checkout", "payment"):
        fx.write(f"jaeger/traces_{svc}.json", envelope(failing + healthy))
    pay, chk = [], []
    for i in range(6):
        t = NOW - 120 + 15 * i
        pay.append(f"{iso(t)} " + json.dumps({"level": 30, "time": t * 1000, "pid": 1, "hostname": "payment", "msg": "Charge request received.", "request": {"amount": {"currencyCode": "USD", "units": 120 + i}}}))
        pay.append(f"{iso(t + 0.01)} " + json.dumps({"level": 50, "time": t * 1000 + 10, "pid": 1, "hostname": "payment", "msg": PAY_ERR, "err": {"type": "Error", "message": PAY_ERR}}))
        chk.append(f"{iso(t + 0.02)} " + json.dumps({"level": "error", "msg": f"failed to charge card: could not charge the card: rpc error: code = Unknown desc = {PAY_ERR}", "time": iso(t + 0.02, False)}))
    fx.write("docker/logs/payment.log", "\n".join(pay) + "\n")
    fx.write("docker/logs/checkout.log", "\n".join(chk) + "\n")
    fx.write("docker/logs/frontend.log", "\n".join(f"{iso(NOW - 110 + 15 * i)} Error: 13 INTERNAL: failed to charge card" for i in range(6)) + "\n")
    fx.write("docker/logs/email.log", "")
    fx.write("docker/logs/cart.log", "")


# --- b. product-catalog fails for one product id ------------------------------------------------------------
BAD_ID, GOOD_IDS = "OLJCESPC7Z", ["66VCHSJNUP", "1YMWWN1N4O", "L9ECAV7KIM", "2ZYFJ3GM2N"]


def product_one_item():
    fx = Fixture("product_one_item")
    rps = {s: r for s, (r, _) in BASE.items()}
    common(fx, "product-catalog GetProduct fails for one product id; container healthy",
           {"incident_id": "INC-APP-PRODUCT", "detected_at": iso(NOW, False), "alerting_services": ["product-catalog"],
            "trigger": [{"service": "product-catalog", "signal": "error_rate", "value": 0.097, "threshold": 0.05, "window": "3m"}],
            "note": "alerting service is a symptom, not necessarily the root cause"},
           server_rps=rps, server_err={"product-catalog": 0.58, "frontend": 0.31, "frontend-proxy": 0.31},
           client_rps={"frontend-proxy": 9.8, "frontend": 15.2, "recommendation": 1.5, "checkout": 2.5},
           client_err={"frontend": 0.31, "recommendation": 0.12},
           p95={s: p for s, (_, p) in BASE.items()})

    def trace(n, t, pid):
        tid = f"6c2d{n:028x}"
        s = lambda k: f"{n:02x}{k:014x}"  # noqa: E731
        bad = pid == BAD_ID
        err = f"Error: product {pid} could not be loaded: query returned an invalid row" if bad else None
        up = f"13 INTERNAL: {err}" if bad else None
        return {"traceID": tid, "processes": processes(), "warnings": None, "spans": [
            span(tid, s(1), None, "p2", f"GET /api/products/{pid}", "server", t, 21000, up,
                 [("http.response.status_code", 500 if bad else 200)]),
            span(tid, s(2), s(1), "p2", "oteldemo.ProductCatalogService/GetProduct", "client", t + 800, 9000, up,
                 [("rpc.service", "oteldemo.ProductCatalogService"), ("server.address", "product-catalog")]),
            span(tid, s(3), s(2), "p5", "oteldemo.ProductCatalogService/GetProduct", "server", t + 1100, 4100, err,
                 [("rpc.system", "grpc"), ("app.product.id", pid)]),
        ]}

    failing = [trace(i, (NOW - 150 + 20 * i) * 1_000_000, BAD_ID) for i in range(6)]
    healthy = [trace(40 + i, (NOW - 140 + 20 * i) * 1_000_000, GOOD_IDS[i % 4]) for i in range(8)]
    for svc in ("frontend", "frontend-proxy", "product-catalog"):
        fx.write(f"jaeger/traces_{svc}.json", envelope(failing + healthy))
    fx.write("docker/logs/product-catalog.log", "\n".join(
        f"{iso(NOW - 150 + 20 * i)} " + json.dumps({"level": "error", "msg": f"failed to get product {BAD_ID}: query returned an invalid row"})
        for i in range(6)) + "\n")
    fx.write("docker/logs/frontend.log", "\n".join(
        f"{iso(NOW - 149 + 20 * i)} Error: 13 INTERNAL: product {BAD_ID} could not be loaded" for i in range(6)) + "\n")
    fx.write("docker/logs/recommendation.log", "")


# --- c. traffic flood at the entry points -------------------------------------------------------------------
def homepage_flood():
    fx = Fixture("homepage_flood")
    mult = {"frontend-proxy": 5.0, "frontend": 5.0, "product-catalog": 4.0, "currency": 4.0, "image-provider": 4.5,
            "ad": 4.0, "recommendation": 4.0, "cart": 1.2}
    rps = {s: round(r * mult.get(s, 1.0), 3) for s, (r, _) in BASE.items()}
    p95 = {s: p for s, (_, p) in BASE.items()}
    p95.update({"frontend-proxy": 920, "frontend": 880, "product-catalog": 22, "recommendation": 140, "ad": 45})
    common(fx, "request rate several times the baseline at the entry points; containers healthy",
           {"incident_id": "INC-APP-FLOOD", "detected_at": iso(NOW, False), "alerting_services": ["frontend", "frontend-proxy"],
            "trigger": [{"service": "frontend-proxy", "signal": "p95_ms", "value": 920.0, "threshold": 500.0, "window": "3m"},
                        {"service": "frontend", "signal": "p95_ms", "value": 880.0, "threshold": 500.0, "window": "3m"}],
            "note": "alerting service is a symptom, not necessarily the root cause"},
           server_rps=rps, server_err={"frontend-proxy": 0.12, "frontend": 0.1},
           client_rps={"load-generator": 49.0, "frontend-proxy": 49.0, "frontend": 80.0},
           client_err={"load-generator": 0.12}, p95=p95,
           cpu={"frontend": 88.0, "frontend-proxy": 61.0, "product-catalog": 40.0},
           range_rps={"frontend-proxy": lambda i: round(9.6 + (48 - 9.6) * min(i, 20) / 20, 2),
                      "frontend": lambda i: round(9.0 + (45 - 9.0) * min(i, 20) / 20, 2)})

    def trace(n, t, dur_ms):
        tid = f"7d3e{n:028x}"
        s = lambda k: f"{n:02x}{k:014x}"  # noqa: E731
        return {"traceID": tid, "processes": processes(), "warnings": None, "spans": [
            span(tid, s(1), None, "p1", "ingress", "server", t, dur_ms * 1000, None, [("http.response.status_code", 200)]),
            span(tid, s(2), s(1), "p2", "GET /", "server", t + 400, (dur_ms - 30) * 1000, None, [("http.route", "/")]),
            span(tid, s(3), s(2), "p2", "oteldemo.ProductCatalogService/ListProducts", "client", t + 2000, 21000, None,
                 [("rpc.service", "oteldemo.ProductCatalogService"), ("server.address", "product-catalog")]),
            span(tid, s(4), s(3), "p5", "oteldemo.ProductCatalogService/ListProducts", "server", t + 2300, 19000),
        ]}

    slow = [trace(i, (NOW - 120 + 10 * i) * 1_000_000, 850 + 10 * i) for i in range(10)]
    for svc in ("frontend", "frontend-proxy"):
        fx.write(f"jaeger/traces_{svc}.json", envelope(slow))
    fx.write("docker/logs/frontend-proxy.log", "\n".join(
        f'{iso(NOW - 60 + i)} [{iso(NOW - 60 + i, False)}] "GET / HTTP/1.1" 200 - 0 30212 {900 + i} "-" "python-requests/2.32"'
        for i in range(20)) + "\n")
    fx.write("docker/logs/frontend.log", "")


# --- d. two unrelated faults at once (built from a and b) ------------------------------------------------------
def two_app_faults():
    """payment's Charge handler fails AND product-catalog fails for one product. Run a and b first."""
    fx = Fixture("two_app_faults")
    rps = {s: r for s, (r, _) in BASE.items()}
    rps.update(email=0.03)
    common(fx, "two unrelated faults: payment's Charge handler errors and product-catalog fails for one product id",
           {"incident_id": "INC-APP-TWO", "detected_at": iso(NOW, False), "alerting_services": ["checkout", "product-catalog"],
            "trigger": [{"service": "checkout", "signal": "error_rate", "value": 0.93, "threshold": 0.05, "window": "3m"},
                        {"service": "product-catalog", "signal": "error_rate", "value": 0.097, "threshold": 0.05, "window": "3m"}],
            "note": "alerting service is a symptom, not necessarily the root cause"},
           server_rps=rps, server_err={"payment": 0.37, "checkout": 0.38, "product-catalog": 0.58, "frontend": 0.69, "frontend-proxy": 0.69},
           client_rps={"frontend-proxy": 9.8, "frontend": 15.2, "checkout": 2.5, "recommendation": 1.5},
           client_err={"checkout": 0.37, "frontend": 0.69, "recommendation": 0.12}, p95={s: p for s, (_, p) in BASE.items()})
    sources = [HERE / "payment_app_error", HERE / "product_one_item"]
    for svc in ("frontend", "frontend-proxy", "checkout", "payment", "product-catalog"):
        traces = []
        for src in sources:
            path = src / "jaeger" / f"traces_{svc}.json"
            if path.exists():
                traces += json.loads(path.read_text(encoding="utf-8"))["data"]
        fx.write(f"jaeger/traces_{svc}.json", envelope(traces))
    for svc in ("payment", "checkout", "frontend", "email", "cart", "product-catalog", "recommendation"):
        fx.write(f"docker/logs/{svc}.log", "".join(
            (src / "docker" / "logs" / f"{svc}.log").read_text(encoding="utf-8")
            for src in sources if (src / "docker" / "logs" / f"{svc}.log").exists()))


if __name__ == "__main__":
    for build in (payment_app_error, product_one_item, homepage_flood, two_app_faults):
        build()
        print(f"wrote {build.__name__}")
