"""get_metric / list_metrics: ad-hoc Prometheus queries, summarised.

get_metric returns min / max / first / latest / trend per series instead of raw samples.
list_metrics finds metric names (runtime CPU, memory, GC metrics differ per language).

CLI: python -m oncall.tools.metrics query 'process_cpu_utilization' --window 15m
     python -m oncall.tools.metrics list memory
"""
import argparse

from oncall.evidence import registry
from oncall.sources import get_source
from oncall.sources.guard import check
from oncall.tools._common import cap, fmt_num, parse_window, run_cli

MAX_SERIES = 8


def _trend(first, latest, lo, hi):
    if first is None or latest is None:
        return "unknown"
    span = (hi - lo) or abs(first) or 1.0
    change = (latest - first) / span
    if change > 0.5:
        return "rising"
    if change < -0.5:
        return "falling"
    return "flat"


def get_metric(promql, window="15m"):
    check(promql)
    window_s = parse_window(window, default=900)
    step = max(window_s // 30, 15)
    series = get_source().prom_range(promql, window_s, step)
    rows = []
    for s in series:
        values = [float(v) for _, v in s.get("values", []) if v not in ("NaN", "+Inf", "-Inf")]
        if not values:
            continue
        lo, hi = min(values), max(values)
        rows.append({
            "labels": {k: v for k, v in s["metric"].items() if k != "__name__"},
            "min": fmt_num(lo), "max": fmt_num(hi),
            "first": fmt_num(values[0]), "latest": fmt_num(values[-1]),
            "trend": _trend(values[0], values[-1], lo, hi), "points": len(values),
        })
    rows.sort(key=lambda r: abs(r["max"]), reverse=True)
    rows = rows[:MAX_SERIES]
    for r in rows:
        label = ",".join(f"{k}={v}" for k, v in r["labels"].items()) or "(no labels)"
        r["evidence"] = registry.add(
            "get_metric", "metric",
            f"{promql} [{label}] over {window_s}s: first {r['first']}, latest {r['latest']}, "
            f"min {r['min']}, max {r['max']} ({r['trend']})",
            {"promql": promql, "window_s": window_s, "step_s": step, **r})
    result = {"promql": promql, "window_s": window_s, "series_total": len(series), "series": rows}
    if not rows:
        result["evidence"] = registry.add("get_metric", "metric",
                                          f"{promql} returned no data over {window_s}s",
                                          {"promql": promql, "window_s": window_s})
    return cap(result)


def list_metrics(contains="", limit=60):
    names = get_source().prom_names()
    words = [w.lower() for w in contains.split()] if contains else []
    hits = [n for n in names if all(w in n.lower() for w in words)]
    return cap({"contains": contains, "total": len(hits), "names": hits[:limit]})


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("query")
    q.add_argument("promql")
    q.add_argument("--window", default="15m")
    ls = sub.add_parser("list")
    ls.add_argument("contains", nargs="?", default="")
    # Shared flags (--source, --bundle, ...) go before the subcommand.

    def call(cmd, **kw):
        return get_metric(**kw) if cmd == "query" else list_metrics(**kw)

    run_cli(call, p, lambda a: ({"cmd": "query", "promql": a.promql, "window": a.window}
                                if a.cmd == "query" else {"cmd": "list", "contains": a.contains}))


if __name__ == "__main__":
    main()
