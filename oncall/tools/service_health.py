"""get_service_health: error rate, request rate and p95 latency per service, worst first.

Uses the span metrics produced from traces. With a saved baseline (--save-baseline, captured
before any fault) every row also shows before/after numbers.

CLI: python -m oncall.tools.service_health --window 5m [--save-baseline]
"""
import argparse
import json
import math

from oncall import config
from oncall.evidence import registry
from oncall.sources import get_source
from oncall.sources.bundle import BundleSource
from oncall.tools._common import cap, fmt_num, metric_names, parse_window, prom_window, run_cli

TOOL = "get_service_health"

# Investigation flags (looser than alert thresholds: we want to see weak signals too).
ERR_MIN = 0.01          # 1 % errors
ERR_X_BASELINE = 3.0
SLOW_RATIO = 2.0        # p95 vs baseline
SLOW_MIN_MS = 50.0
SPIKE_RATIO = 3.0
DROP_RATIO = 0.3


def _by_service(query):
    out = {}
    for series in get_source().prom_query(query):
        svc = series["metric"].get(config.SERVICE_LABEL)
        if not svc:
            continue
        val = float(series["value"][1])
        out[svc] = None if math.isnan(val) or math.isinf(val) else val
    return out


def _queries(window_s, kind_filter):
    names = metric_names()
    calls, dur = names["calls"], names["duration"]
    if not calls:
        raise RuntimeError("no span-metrics 'calls' metric found in Prometheus; is the "
                           "observability layer running? Set metric names in config.py")
    w, sv, st = prom_window(window_s), config.SERVICE_LABEL, config.STATUS_LABEL
    server = f'{config.SPAN_KIND_LABEL}=~"{"|".join(config.SERVER_KINDS)}"' if kind_filter else ""
    client = f'{config.SPAN_KIND_LABEL}="SPAN_KIND_CLIENT"' if kind_filter else ""
    err = f'{st}="{config.ERROR_STATUS}"'

    def sel(*parts):
        parts = [p for p in parts if p]
        return "{" + ",".join(parts) + "}" if parts else ""

    q = {
        "rps": f"sum by ({sv}) (rate({calls}{sel(server)}[{w}]))",
        "err_rps": f"sum by ({sv}) (rate({calls}{sel(server, err)}[{w}]))",
    }
    if kind_filter:
        q["client_rps"] = f"sum by ({sv}) (rate({calls}{sel(client)}[{w}]))"
        q["client_err_rps"] = f"sum by ({sv}) (rate({calls}{sel(client, err)}[{w}]))"
    if dur:
        q["p95_ms"] = (f"histogram_quantile(0.95, sum by ({sv}, le) "
                       f"(rate({dur}{sel(server)}[{w}])))")
    return q


def compute_health(window="5m"):
    """Raw per-service numbers, no evidence registration. Used by the watcher and baseline."""
    window_s = parse_window(window)
    queries = _queries(window_s, kind_filter=True)
    data = {k: _by_service(q) for k, q in queries.items()}
    if not data["rps"]:  # span_kind label missing in this version: retry without it
        queries = _queries(window_s, kind_filter=False)
        data = {k: _by_service(q) for k, q in queries.items()}

    services = {}
    for svc in set().union(*[d.keys() for d in data.values()]):
        rps = data["rps"].get(svc) or 0.0
        err = data["err_rps"].get(svc) or 0.0
        c_rps = data.get("client_rps", {}).get(svc) or 0.0
        c_err = data.get("client_err_rps", {}).get(svc) or 0.0
        services[svc] = {
            "rps": fmt_num(rps),
            "error_rate": fmt_num(err / rps if rps else 0.0, 4),
            "client_error_rate": fmt_num(c_err / c_rps if c_rps else 0.0, 4),
            "p95_ms": fmt_num(data.get("p95_ms", {}).get(svc), 1),
        }
    return {"window_s": window_s, "captured_at": get_source().now(),
            "queries": queries, "services": services}


def load_baseline():
    src = get_source()
    if isinstance(src, BundleSource) and (src.dir / "baseline.json").exists():
        return json.loads((src.dir / "baseline.json").read_text())
    if config.BASELINE_FILE.exists():
        return json.loads(config.BASELINE_FILE.read_text())
    return None


def save_baseline(window="5m"):
    health = compute_health(window)
    config.BASELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.BASELINE_FILE.write_text(json.dumps(health, indent=2))
    return {"saved": str(config.BASELINE_FILE), "services": len(health["services"])}


def _ratio(now, before):
    if now is None or before in (None, 0):
        return None
    return round(now / before, 2)


def _assess(svc, row, base):
    flags = []
    b = (base or {}).get(svc)
    if b:
        row["baseline"] = {k: b.get(k) for k in ("rps", "error_rate", "client_error_rate", "p95_ms")}
        row["p95_ratio"] = _ratio(row["p95_ms"], b.get("p95_ms"))
        row["rps_ratio"] = _ratio(row["rps"], b.get("rps"))

    def worse(key):
        before = (b or {}).get(key) or 0.0
        return row[key] >= ERR_MIN and row[key] >= before * ERR_X_BASELINE

    if worse("error_rate"):
        flags.append("errors")
    if worse("client_error_rate"):
        flags.append("client_call_errors")
    p95 = row["p95_ms"]
    if p95 is not None and p95 >= SLOW_MIN_MS and (
            p95 >= config.ALERT_P95_MS or (row.get("p95_ratio") or 0) >= SLOW_RATIO):
        flags.append("slow")
    if row.get("rps_ratio") is not None:
        if row["rps_ratio"] >= SPIKE_RATIO:
            flags.append("traffic_spike")
        elif row["rps_ratio"] <= DROP_RATIO:
            flags.append("traffic_drop")
    row["flags"] = flags
    row["criticality"] = config.CRITICALITY.get(svc, "unknown")
    crit_bonus = {"critical": 3, "high": 2, "medium": 1}.get(row["criticality"], 0)
    row["_score"] = (row["error_rate"] * 100 + row["client_error_rate"] * 50
                     + min((row.get("p95_ratio") or 1) - 1, 20) * 3
                     + len(flags) * 10 + crit_bonus)
    return row


def get_service_health(window="5m", compare_baseline=True):
    health = compute_health(window)
    base = load_baseline() if compare_baseline else None
    base_services = (base or {}).get("services", {})
    rows = []
    for svc, row in health["services"].items():
        rows.append({"service": svc, **_assess(svc, dict(row), base_services)})

    # Services that served traffic in the baseline but have no server spans now.
    for svc, b in base_services.items():
        if (b.get("rps") or 0) >= config.ALERT_MIN_RPS and \
                (health["services"].get(svc, {}).get("rps") or 0) < config.ALERT_MIN_RPS:
            existing = next((r for r in rows if r["service"] == svc), None)
            if existing:
                existing["flags"].append("silent")
                existing["_score"] += 80
            else:
                rows.append({"service": svc, "rps": 0.0, "error_rate": None, "p95_ms": None,
                             "baseline": b, "flags": ["silent"], "_score": 80,
                             "criticality": config.CRITICALITY.get(svc, "unknown")})

    rows.sort(key=lambda r: r["_score"], reverse=True)
    window_s = health["window_s"]
    for r in rows:
        r.pop("_score")
        if r["flags"]:
            r["evidence"] = registry.add(TOOL, "metric", _summary(r, window_s),
                                         {"row": r, "queries": health["queries"],
                                          "window_s": window_s})
    flagged = [r for r in rows if r["flags"]]
    healthy = [r["service"] for r in rows if not r["flags"]]
    if not flagged:
        eid = registry.add(TOOL, "metric",
                           f"no service breaches investigation thresholds over {window_s}s",
                           {"services": health["services"]})
    result = {
        "window_s": window_s,
        "baseline": "loaded" if base else "none (run with --save-baseline before faults)",
        "flagged": flagged[:10],
        "healthy": healthy,
    }
    if not flagged:
        result["evidence"] = eid
    return cap(result)


def _summary(r, window_s):
    parts = [f"{r['service']} [{','.join(r['flags'])}] over {window_s}s:"]
    b = r.get("baseline") or {}

    def show(label, key, unit="", pct=False):
        now = r.get(key)
        if now is None:
            return
        fmt = (lambda v: f"{v * 100:.1f}%") if pct else (lambda v: f"{v}{unit}")
        text = f"{label} {fmt(now)}"
        if b.get(key) is not None:
            text += f" (baseline {fmt(b[key])})"
        parts.append(text)

    show("error_rate", "error_rate", pct=True)
    show("client_error_rate", "client_error_rate", pct=True)
    show("p95", "p95_ms", "ms")
    show("rps", "rps")
    return " ".join(parts)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--window", default="5m")
    p.add_argument("--no-baseline", action="store_true")
    p.add_argument("--save-baseline", action="store_true",
                   help="capture healthy numbers to runs/baseline.json (do this before faults)")

    def call(**kw):
        if kw.pop("save_baseline"):
            return save_baseline(kw["window"])
        return get_service_health(**kw)

    run_cli(call, p, lambda a: {"window": a.window, "compare_baseline": not a.no_baseline,
                                "save_baseline": a.save_baseline})


if __name__ == "__main__":
    main()
