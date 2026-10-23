"""Threshold watcher (no LLM). Polls span metrics and fires an alert after two consecutive breaches."""
import time
from datetime import datetime, timezone

from oncall import config
from oncall.tools.metrics import compute_health, load_baseline

CONSECUTIVE = 2


def breaches(health, baseline):
    found = []
    base = (baseline or {}).get("services", {})
    for svc, row in health["services"].items():
        if (row["rps"] or 0) < config.ALERT_MIN_RPS:
            continue
        if row["error_rate"] >= config.ALERT_ERROR_RATE:
            found.append({"service": svc, "signal": "error_rate", "value": row["error_rate"],
                          "threshold": config.ALERT_ERROR_RATE})
        p95, b95 = row["p95_ms"], (base.get(svc) or {}).get("p95_ms")
        if p95 is not None and p95 >= config.ALERT_P95_MS:
            found.append({"service": svc, "signal": "p95_latency_ms", "value": p95,
                          "threshold": config.ALERT_P95_MS})
        elif p95 is not None and b95 and p95 >= b95 * config.ALERT_P95_X_BASELINE and p95 >= 100:
            found.append({"service": svc, "signal": "p95_latency_vs_baseline", "value": p95,
                          "threshold": round(b95 * config.ALERT_P95_X_BASELINE, 1)})
    for svc, b in base.items():
        if (b.get("rps") or 0) >= config.ALERT_MIN_RPS * 4 and \
                (health["services"].get(svc, {}).get("rps") or 0) < config.ALERT_MIN_RPS:
            found.append({"service": svc, "signal": "traffic_stopped", "value": 0,
                          "threshold": b.get("rps")})
    crit = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    found.sort(key=lambda b: (crit.get(config.CRITICALITY.get(b["service"]), 4), -float(b["value"] or 0)))
    return found


def make_alert(found, window_s):
    top = found[0]
    return {
        "source": "watcher", "fired_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "service": top["service"], "signal": top["signal"], "value": top["value"],
        "threshold": top["threshold"], "window_s": window_s,
        "other_breaches": found[1:8],
    }


def watch(on_alert, interval=15, window="3m", cooldown=600, once=False):
    baseline = load_baseline()
    if not baseline:
        print("warning: no baseline; latency-vs-baseline and traffic_stopped checks are off")
    streak = {}
    last_fired = 0.0
    while True:
        try:
            health = compute_health(window)
            found = breaches(health, baseline)
        except Exception as e:  # keep watching through transient errors
            print(f"[watch] health check failed: {e}")
            found, health = [], {"window_s": 0}
        keys = {(b["service"], b["signal"]) for b in found}
        streak = {k: streak.get(k, 0) + 1 for k in keys}
        firing = [b for b in found if streak[(b["service"], b["signal"])] >= CONSECUTIVE]
        stamp = datetime.now().strftime("%H:%M:%S")
        print(f"[watch {stamp}] breaches: {len(found)} firing: {len(firing)}", flush=True)
        if firing and time.time() - last_fired >= cooldown:
            last_fired = time.time()
            on_alert(make_alert(firing, health["window_s"]))
            if once:
                return
        time.sleep(interval)
