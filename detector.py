"""The one incident detector (no LLM): detect an incident in the OpenTelemetry demo and start
an investigation. `python -m oncall watch` and scripts/demo.py use it too.

Polls Prometheus, computes per-service error rate and p95 latency of incoming requests and
request rate from the demo's span metrics, and fires ONE grouped incident when a threshold is
breached for several polls in a row.

Error rate uses a fixed limit. Latency and silence use each service's OWN healthy baseline
(calibrated), because fixed limits are noisy and traffic levels differ per service.

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
# Silent: traffic below SILENT_RATIO of the service's own baseline rps. Services whose
# baseline is below SILENT_MIN_BASE_RPS are idle anyway and never flagged.
SILENT_RATIO = float(os.getenv("SILENT_RATIO", "0.2"))
SILENT_MIN_BASE_RPS = float(os.getenv("SILENT_MIN_BASE_RPS", "0.005"))
# Highest finite histogram bucket. p95 at or above it only says "beyond the histogram", so
# calibration ignores it. Discovered from Prometheus; this is the fallback.
TOP_BUCKET_MS = float(os.getenv("P95_TOP_BUCKET_MS", "15000"))
CONSECUTIVE = int(os.getenv("CONSECUTIVE", "2"))           # breaches in a row before alerting
COOLDOWN_SECS = int(os.getenv("COOLDOWN_SECS", "300"))     # per service+signal re-alert delay
HERE = Path(__file__).resolve().parent
BASELINE_FILE = Path(os.getenv("BASELINE_FILE", HERE / "baseline.json"))
INCIDENTS_FILE = Path(os.getenv("INCIDENTS_FILE", HERE / "incidents.jsonl"))
# Traffic generator and browser-side spans are not part of the system being diagnosed.
# flagd/flagd-ui are excluded so flag services stay out of the incident picture (HARD RULE 2).
EXCLUDE = {s for s in os.getenv("EXCLUDE", "load-generator,frontend-web,flagd,flagd-ui").split(",") if s}

# Metric names produced by the demo's spanmetrics connector (verified working).
CALLS = os.getenv("CALLS_METRIC", "traces_span_metrics_calls_total")
DURATION = os.getenv("DURATION_METRIC", "traces_span_metrics_duration_milliseconds_bucket")
SVC = os.getenv("SERVICE_LABEL", "service_name")

# Error rate and p95 count only incoming requests (server spans): a failed outgoing call is
# the callee's problem and is counted there, and long-lived client spans (e.g. streaming
# connections) are not request latency. rps stays on all spans.
SERVER = 'span_kind="SPAN_KIND_SERVER"'
QUERIES = {
    "error_rate": (
        f'sum by ({SVC}) (rate({CALLS}{{{SERVER},status_code="STATUS_CODE_ERROR"}}[{WINDOW}]))'
        f" / sum by ({SVC}) (rate({CALLS}{{{SERVER}}}[{WINDOW}]))"
    ),
    "p95_ms": (
        f"histogram_quantile(0.95, sum by (le, {SVC}) (rate({DURATION}{{{SERVER}}}[{WINDOW}])))"
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


def top_bucket_ms():
    """Highest finite `le` of the duration histogram, or TOP_BUCKET_MS if it can't be read."""
    try:
        r = requests.get(f"{PROM}/api/v1/label/le/values", params={"match[]": DURATION}, timeout=10)
        bounds = [float(v) for v in r.json().get("data", []) if v != "+Inf"]
        return max(bounds) if bounds else TOP_BUCKET_MS
    except (requests.RequestException, ValueError):
        return TOP_BUCKET_MS


# ---------------------------------------------------------------- BASELINE
def load_baseline():
    """{service: {"p95_ms": float | None, "rps": float | None}}. p95 None = unknown."""
    try:
        with open(BASELINE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        return {}
    if "services" in data:
        return data["services"]
    # Older files held only {"p95_ms": {service: value}}.
    return {svc: {"p95_ms": v, "rps": None} for svc, v in data.get("p95_ms", {}).items()}


def _median(values):
    values = sorted(values)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def p95_limit(service, baseline):
    """Latency limit for a service, or None when it has no baseline yet.

    Known baseline: LATENCY_MULT x its healthy max p95. Unknown (only saturated samples during
    calibration): LATENCY_MULT x the median of the other services' baselines, so alerts stay on.
    """
    entry = baseline.get(service)
    if entry is None:
        return None
    b = entry.get("p95_ms")
    if b is None:
        b = _median([e["p95_ms"] for e in baseline.values() if e.get("p95_ms") is not None])
        if b is None:
            return P95_FLOOR_MS
    return max(P95_FLOOR_MS, LATENCY_MULT * b)


def learn_baseline(samples, top_ms):
    """samples: {service: {"p95": [...], "rps": [...]}} -> baseline entries.

    p95 samples at or above the top histogram bucket are ignored; a service left with none
    gets p95_ms None ("unknown"). rps baseline is the median of the samples.
    """
    out = {}
    for svc, s in samples.items():
        p95 = [p for p in s["p95"] if math.isfinite(p) and p < top_ms]
        rps = [r for r in s["rps"] if math.isfinite(r)]
        # The max p95 is deliberately conservative: fewer false alarms on a healthy system.
        out[svc] = {"p95_ms": round(max(p95), 1) if p95 else None,
                    "rps": round(_median(rps), 4) if rps else None}
    return out


def calibrate(seconds):
    print(f"calibrating for {seconds}s. Keep every feature flag OFF.", flush=True)
    top_ms = top_bucket_ms()
    samples = {}
    end = time.time() + seconds
    while True:
        snap = snapshot()
        for svc, rps in snap["rps"].items():
            if svc in EXCLUDE or rps < MIN_RPS:
                continue
            s = samples.setdefault(svc, {"p95": [], "rps": []})
            s["rps"].append(rps)
            if svc in snap["p95_ms"]:
                s["p95"].append(snap["p95_ms"][svc])
        if time.time() >= end:
            break
        time.sleep(POLL_SECS)
    baseline = learn_baseline(samples, top_ms)
    with open(BASELINE_FILE, "w") as f:
        json.dump({"created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "window": WINDOW, "top_bucket_ms": top_ms, "services": baseline}, f, indent=2)
    print(f"saved {BASELINE_FILE} (top histogram bucket {top_ms:g} ms)")
    for svc in sorted(baseline):
        b = baseline[svc]
        p95 = "unknown" if b["p95_ms"] is None else f"{b['p95_ms']:.1f} ms"
        print(f"  {svc:24} baseline p95 {p95:>10} -> alert above {p95_limit(svc, baseline):.0f} ms"
              f" | rps {b['rps']} -> silent below {SILENT_RATIO * (b['rps'] or 0):.4f}")


# ---------------------------------------------------------------- DETECTION
def silent_threshold(service, baseline, peaks):
    """rps below which the service counts as silent, or None if it is not checked."""
    ref = (baseline.get(service) or {}).get("rps") or (peaks or {}).get(service)
    if ref is None:
        # Older baselines without rps: a service with a baseline had traffic, use MIN_RPS.
        return MIN_RPS if service in baseline else None
    return SILENT_RATIO * ref if ref >= SILENT_MIN_BASE_RPS else None


def poll(baseline, peaks=None):
    """Return (breaches, snapshot). A breach is a service crossing a threshold.

    peaks: highest rps seen per service in this process, the silence reference for services
    that have no baseline rps.
    """
    snap = snapshot()
    breaches = []
    for svc in sorted(set(baseline) | set(peaks or {})):
        if svc in EXCLUDE:
            continue
        threshold = silent_threshold(svc, baseline, peaks)
        rps = snap["rps"].get(svc, 0.0)
        if threshold is not None and rps < threshold:
            breaches.append(breach(svc, "silent", rps, threshold))
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
    """Default hand-off: print and log. `python -m oncall watch` passes the agent instead."""
    print(json.dumps(incident, indent=2), flush=True)
    with open(INCIDENTS_FILE, "a") as f:
        f.write(json.dumps(incident) + "\n")


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


def detect_once():
    """One poll with the calibrated limits: an incident dict, or None if nothing breaches."""
    breaches, _ = poll(load_baseline())
    return make_incident(breaches) if breaches else None


# ---------------------------------------------------------------- MAIN LOOP
def run(on_incident=start_investigation, once=False):
    """Poll forever; hand ONE grouped incident to on_incident when breaches persist for
    CONSECUTIVE polls. once=True returns after the first incident."""
    baseline = load_baseline()
    peaks = {}        # service -> highest rps seen (silence reference without a baseline)
    streak = {}       # (service, signal) -> consecutive breach count
    last_fired = {}   # (service, signal) -> unix time of last alert
    print(f"watching {PROM} every {POLL_SECS}s | error_rate>{ERR_RATE_MAX} | "
          f"silent<{SILENT_RATIO:.0%} of baseline rps | need {CONSECUTIVE} polls in a row", flush=True)
    if baseline:
        print(f"latency + silence alerts ON ({len(baseline)} services, from {BASELINE_FILE})", flush=True)
    else:
        print(f"WARNING: no {BASELINE_FILE}. Latency alerts are OFF. "
              "Run: python detector.py --calibrate", flush=True)
    while True:
        try:
            breaches, snap = poll(baseline, peaks)
            for s, r in snap["rps"].items():
                if s not in EXCLUDE and r >= MIN_RPS:
                    peaks[s] = max(peaks.get(s, 0.0), r)
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
            on_incident(make_incident(ready))  # ONE incident, many signals
            if once:
                return
        time.sleep(POLL_SECS)


def probe():
    baseline = load_baseline()
    breaches, snap = poll(baseline)
    services = sorted(set(snap["rps"]) | set(snap["error_rate"]) | set(snap["p95_ms"]) | set(baseline))
    if not services:
        print("No data. Check PROM_URL and the metric names (search 'span_metrics' in Prometheus).")
        return
    flagged = {b["service"] for b in breaches}
    print(f"{'service':24} {'rps':>7} {'err%':>7} {'p95 ms':>9} {'p95 limit':>10} {'silent<':>8}")
    for s in services:
        lim = p95_limit(s, baseline)
        quiet = silent_threshold(s, baseline, None)
        note = " (excluded)" if s in EXCLUDE else (" <-- BREACH" if s in flagged else "")
        print(f"{s:24} {snap['rps'].get(s, 0):7.2f} {snap['error_rate'].get(s, 0) * 100:7.2f} "
              f"{snap['p95_ms'].get(s, float('nan')):9.1f} "
              f"{('-' if lim is None else f'{lim:.0f}'):>10} "
              f"{('-' if quiet is None else f'{quiet:.3f}'):>8}{note}")
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
