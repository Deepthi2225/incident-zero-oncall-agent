"""Checkpoint 1: detect an incident in the OpenTelemetry demo and start an investigation.

Polls Prometheus, computes per-service error rate and p95 latency from the demo's
span metrics, and fires ONE grouped incident when a threshold is breached for
several polls in a row.

Error rate uses a fixed limit. Latency uses each service's OWN healthy baseline,
because a fixed limit is noisy (p95 mixes every span type a service emits).

Usage:
    python detector.py --probe            # show services, values and limits
    python detector.py --calibrate [secs] # learn healthy latency (default 300s)
    python detector.py                    # run the detector loop
    python detector.py --once             # one poll, print breaches, exit

Calibrate once, after the stack has run ~10 minutes with NO feature flags on.
All settings can be overridden with environment variables (see CONFIG).
"""
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------- CONFIG
PROM = os.getenv("PROM_URL", "http://localhost:9090")
# The demo exports metrics about once a minute, so the rate() window must span
# several samples. 3m is the practical minimum; 1m returns empty results.
POLL_SECS = int(os.getenv("POLL_SECS", "30"))
WINDOW = os.getenv("WINDOW", "3m")
ERR_RATE_MAX = float(os.getenv("ERR_RATE_MAX", "0.05"))    # 5% of requests failing
P95_FLOOR_MS = float(os.getenv("P95_FLOOR_MS", "500"))     # never alert on p95 below this
LATENCY_MULT = float(os.getenv("LATENCY_MULT", "2.0"))     # x the service's healthy max p95
MIN_RPS = float(os.getenv("MIN_RPS", "0.02"))              # ignore (near) idle services
CONSECUTIVE = int(os.getenv("CONSECUTIVE", "2"))           # breaches in a row before alerting
COOLDOWN_SECS = int(os.getenv("COOLDOWN_SECS", "300"))     # per service+signal re-alert delay
HERE = Path(__file__).resolve().parent
BASELINE_FILE = Path(os.getenv("BASELINE_FILE", HERE / "baseline.json"))
INCIDENTS_FILE = Path(os.getenv("INCIDENTS_FILE", HERE / "incidents.jsonl"))
# Traffic generator and browser-side spans are not part of the system being diagnosed.
EXCLUDE = {s for s in os.getenv("EXCLUDE", "load-generator,frontend-web").split(",") if s}

# Metric names produced by the demo's spanmetrics connector (verified working).
CALLS = os.getenv("CALLS_METRIC", "traces_span_metrics_calls_total")
DURATION = os.getenv("DURATION_METRIC", "traces_span_metrics_duration_milliseconds_bucket")
SVC = os.getenv("SERVICE_LABEL", "service_name")

# Error rate counts only incoming requests (server spans); a failed outgoing call is the
# callee's problem and is counted there. rps stays on all spans.
SERVER = 'span_kind="SPAN_KIND_SERVER"'
QUERIES = {
    "error_rate": (
        f'sum by ({SVC}) (rate({CALLS}{{{SERVER},status_code="STATUS_CODE_ERROR"}}[{WINDOW}]))'
        f" / sum by ({SVC}) (rate({CALLS}{{{SERVER}}}[{WINDOW}]))"
    ),
    "p95_ms": (
        f"histogram_quantile(0.95, sum by (le, {SVC}) (rate({DURATION}[{WINDOW}])))"
    ),
    "rps": f"sum by ({SVC}) (rate({CALLS}[{WINDOW}]))",
}


# ---------------------------------------------------------------- PROMETHEUS
def query(expr):
    """Run an instant query, return {service: value}. NaN and +/-Inf results are dropped."""
    r = requests.get(f"{PROM}/api/v1/query", params={"query": expr}, timeout=10)
    r.raise_for_status()
    body = r.json()
    if body.get("status") != "success":
        raise RuntimeError(f"prometheus error: {body}")
    out = {}
    for series in body["data"]["result"]:
        value = float(series["value"][1])
        if math.isfinite(value):
            out[series["metric"].get(SVC, "unknown")] = value
    return out


def snapshot():
    return {name: query(expr) for name, expr in QUERIES.items()}


# ---------------------------------------------------------------- BASELINE
def load_baseline():
    try:
        with open(BASELINE_FILE) as f:
            return json.load(f)["p95_ms"]
    except (FileNotFoundError, KeyError, ValueError):
        return {}


def p95_limit(service, baseline):
    """Latency limit for a service, or None when it has no baseline yet."""
    b = baseline.get(service)
    return None if b is None else max(P95_FLOOR_MS, LATENCY_MULT * b)


def calibrate(seconds):
    print(f"calibrating for {seconds}s. Keep every feature flag OFF.", flush=True)
    seen = {}
    end = time.time() + seconds
    while True:
        snap = snapshot()
        for svc, rps in snap["rps"].items():
            p = snap["p95_ms"].get(svc)
            if rps >= MIN_RPS and p is not None and math.isfinite(p) and svc not in EXCLUDE:
                seen.setdefault(svc, []).append(p)
        if time.time() >= end:
            break
        time.sleep(POLL_SECS)
    # The max is deliberately conservative: fewer false alarms on a healthy system.
    baseline = {svc: round(max(v), 1) for svc, v in seen.items()}
    with open(BASELINE_FILE, "w") as f:
        json.dump({"created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "window": WINDOW, "p95_ms": baseline}, f, indent=2)
    print(f"saved {BASELINE_FILE}")
    for svc in sorted(baseline):
        print(f"  {svc:24} baseline p95 {baseline[svc]:9.1f} ms -> alert above "
              f"{p95_limit(svc, baseline):.0f} ms")


# ---------------------------------------------------------------- DETECTION
def poll(baseline, known=()):
    """Return (breaches, snapshot). A breach is a service crossing a threshold.

    known: services that had traffic before (baseline or earlier polls). One of them with no
    data or rps below MIN_RPS now is a "silent" breach (stopped, crashed or cut off).
    """
    snap = snapshot()
    breaches = []
    for svc in sorted(set(known) | set(baseline)):
        rps = snap["rps"].get(svc, 0.0)
        if svc not in EXCLUDE and rps < MIN_RPS:
            breaches.append(breach(svc, "silent", rps, MIN_RPS))
    for svc, rate in snap["rps"].items():
        if rate < MIN_RPS or svc in EXCLUDE:
            continue
        # A service with no error series simply has no errors, so default to 0.
        e = snap["error_rate"].get(svc, 0.0)
        if e > ERR_RATE_MAX:
            breaches.append(breach(svc, "error_rate", e, ERR_RATE_MAX))
        limit = p95_limit(svc, baseline)
        p = snap["p95_ms"].get(svc)
        if limit is not None and p is not None and p > limit:
            breaches.append(breach(svc, "p95_ms", p, limit))
    return breaches, snap


def breach(service, signal, value, threshold):
    silent = signal == "silent"
    return {
        "service": service,
        "signal": signal,
        "value": round(value, 4),
        "threshold": round(threshold, 4),
        "window": WINDOW,
        "promql": QUERIES["rps" if silent else signal],
        # How far over the line; a silent service has no ratio, so rank it high.
        "severity": 10.0 if silent else round(value / threshold, 2),
    }


# ---------------------------------------------------------------- INCIDENT
def start_investigation(incident):
    """Hand-off point. Checkpoint 2 plugs the agent in here."""
    print(json.dumps(incident, indent=2), flush=True)
    with open(INCIDENTS_FILE, "a") as f:
        f.write(json.dumps(incident) + "\n")
    # Checkpoint 2:
    #   from agent import investigate
    #   investigate(incident)


def make_incident(breaches):
    now = datetime.now(timezone.utc)
    breaches = sorted(breaches, key=lambda b: b["severity"], reverse=True)
    return {
        "incident_id": "INC-" + now.strftime("%Y%m%d-%H%M%S"),
        "detected_at": now.isoformat(timespec="seconds"),
        "trigger": breaches,
        "alerting_services": sorted({b["service"] for b in breaches}),
        # The alerting service shows the SYMPTOM. The root cause is often upstream
        # or downstream, so the agent must confirm it with traces and logs.
        "note": "alerting service is a symptom, not necessarily the root cause",
    }


# ---------------------------------------------------------------- MAIN LOOP
def run():
    baseline = load_baseline()
    known = set(baseline)  # services that have had traffic; silence is checked for these
    streak = {}       # (service, signal) -> consecutive breach count
    last_fired = {}   # (service, signal) -> unix time of last alert
    print(f"watching {PROM} every {POLL_SECS}s | error_rate>{ERR_RATE_MAX} | "
          f"need {CONSECUTIVE} polls in a row", flush=True)
    if baseline:
        print(f"latency alerts ON ({len(baseline)} services, from {BASELINE_FILE})", flush=True)
    else:
        print(f"WARNING: no {BASELINE_FILE}. Latency alerts are OFF. "
              "Run: python detector.py --calibrate", flush=True)
    while True:
        try:
            breaches, snap = poll(baseline, known)
            known |= {s for s, r in snap["rps"].items() if r >= MIN_RPS and s not in EXCLUDE}
        except Exception as ex:  # keep running if Prometheus hiccups
            print(f"[warn] poll failed: {ex}", file=sys.stderr, flush=True)
            time.sleep(POLL_SECS)
            continue

        now = time.time()
        current = {(b["service"], b["signal"]) for b in breaches}
        streak = {k: v for k, v in streak.items() if k in current}  # reset recovered
        ready = []
        for b in breaches:
            k = (b["service"], b["signal"])
            streak[k] = streak.get(k, 0) + 1
            if streak[k] >= CONSECUTIVE and now - last_fired.get(k, 0) > COOLDOWN_SECS:
                ready.append(b)

        if ready:
            for b in ready:
                last_fired[(b["service"], b["signal"])] = now
            start_investigation(make_incident(ready))  # ONE incident, many signals
        time.sleep(POLL_SECS)


def probe():
    baseline = load_baseline()
    breaches, snap = poll(baseline)
    services = sorted(set(snap["rps"]) | set(snap["error_rate"]) | set(snap["p95_ms"]))
    if not services:
        print("No data. Check PROM_URL and the metric names (search 'span_metrics' in Prometheus).")
        return
    flagged = {b["service"] for b in breaches}
    print(f"{'service':24} {'rps':>7} {'err%':>7} {'p95 ms':>9} {'p95 limit':>10}")
    for s in services:
        lim = p95_limit(s, baseline)
        note = " (excluded)" if s in EXCLUDE else (" <-- BREACH" if s in flagged else "")
        print(f"{s:24} {snap['rps'].get(s, 0):7.2f} {snap['error_rate'].get(s, 0) * 100:7.2f} "
              f"{snap['p95_ms'].get(s, float('nan')):9.1f} "
              f"{('-' if lim is None else f'{lim:.0f}'):>10}{note}")
    if not baseline:
        print(f"\nNo {BASELINE_FILE}: latency alerts are off. Run: python detector.py --calibrate")
    print(f"\nbreaches now: {len(breaches)}")
    for b in breaches:
        print(f"  {b['service']}: {b['signal']}={b['value']} (limit {b['threshold']})")


if __name__ == "__main__":
    if "--probe" in sys.argv:
        probe()
    elif "--calibrate" in sys.argv:
        i = sys.argv.index("--calibrate")
        calibrate(int(sys.argv[i + 1]) if len(sys.argv) > i + 1 else 300)
    elif "--once" in sys.argv:
        print(json.dumps(poll(load_baseline())[0], indent=2))
    else:
        run()
