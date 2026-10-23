"""get_deploy_history: recent changes and container health from `docker inspect` (+ `docker stats`).

For every container: status, start/stop times, restart count, OOMKilled, exit code, health check,
and CPU / memory vs limit when stats are available. Problem containers and recent (re)starts are
listed first with evidence; healthy ones are collapsed to names. Our own Action API calls and an
organiser deploy-history file (DEPLOY_HISTORY_FILE, format TBD) are included when present.

CLI: python -m oncall.tools.deploys --lookback 1h
"""
import argparse
import json
import os
import re
from pathlib import Path

from oncall import config
from oncall.evidence import registry
from oncall.sources import SourceError, get_source
from oncall.tools._common import cap, parse_docker_time, parse_window, run_cli

TOOL = "get_deploy_history"
MEM_NEAR_LIMIT = 90.0
CPU_NEAR_LIMIT = 90.0

_SIZE = re.compile(r"([\d.]+)\s*([KMGT]?i?B)", re.I)
_UNIT = {"b": 1, "kb": 1e3, "kib": 1024, "mb": 1e6, "mib": 1024 ** 2, "gb": 1e9, "gib": 1024 ** 3,
         "tb": 1e12, "tib": 1024 ** 4}


def _bytes(text):
    m = _SIZE.search(text or "")
    return float(m.group(1)) * _UNIT.get(m.group(2).lower(), 1) if m else None


def _mb(b):
    return round(b / 1024 ** 2, 1) if b is not None else None


def _pct(text):
    try:
        return float(str(text).strip().rstrip("%"))
    except ValueError:
        return None


def _stats_by_name():
    try:
        rows = get_source().docker_stats()
    except SourceError:
        return {}
    out = {}
    for r in rows:
        usage, _, limit = (r.get("MemUsage") or "").partition("/")
        out[r.get("Name")] = {"cpu_pct": _pct(r.get("CPUPerc")), "mem_pct": _pct(r.get("MemPerc")),
                              "mem_used_mb": _mb(_bytes(usage)), "mem_limit_mb": _mb(_bytes(limit))}
    return out


def describe(inspect, stats, now, lookback_s):
    """One container's state plus a list of human-readable problems."""
    st = inspect.get("State", {})
    host = inspect.get("HostConfig", {})
    name = inspect.get("Name", "").lstrip("/")
    started = parse_docker_time(st.get("StartedAt"))
    finished = parse_docker_time(st.get("FinishedAt"))
    created = parse_docker_time(inspect.get("Created"))
    running = bool(st.get("Running"))
    cores = (host.get("NanoCpus") or 0) / 1e9 or None
    s = stats.get(name, {})
    row = {
        "name": name,
        "status": st.get("Status"),
        "exit_code": st.get("ExitCode"),
        "oom_killed": bool(st.get("OOMKilled")),
        "restart_count": inspect.get("RestartCount", 0),
        "health": (st.get("Health") or {}).get("Status", "none"),
        "started_at": st.get("StartedAt"),
        "started_ago_s": int(now - started) if started else None,
        "image": inspect.get("Config", {}).get("Image"),
    }
    if not running and finished:
        row["stopped_at"] = st.get("FinishedAt")
        row["stopped_ago_s"] = int(now - finished)
    if s:
        row.update(cpu_pct=s.get("cpu_pct"), mem_used_mb=s.get("mem_used_mb"))
        if host.get("Memory"):
            row.update(mem_limit_mb=s.get("mem_limit_mb"), mem_pct=s.get("mem_pct"))

    problems = []
    if row["status"] != "running":
        when = f", stopped {row['stopped_ago_s']}s ago" if "stopped_ago_s" in row else ""
        problems.append(f"not running ({row['status']}, exit code {row['exit_code']}{when})")
    if row["oom_killed"]:
        problems.append("OOMKilled")
    if row["restart_count"]:
        problems.append(f"restarted {row['restart_count']}x")
    if running and row["started_ago_s"] is not None and row["started_ago_s"] <= lookback_s:
        problems.append(f"(re)started {row['started_ago_s']}s ago")
    if created and now - created <= lookback_s:
        problems.append(f"created {int(now - created)}s ago")
    if row["health"] in ("unhealthy", "starting"):
        problems.append(f"health {row['health']}")
    if row.get("mem_pct") is not None and row["mem_pct"] >= MEM_NEAR_LIMIT:
        problems.append(f"memory {row['mem_pct']}% of limit")
    if row.get("cpu_pct") is not None and cores:
        of_limit = row["cpu_pct"] / cores
        if of_limit >= CPU_NEAR_LIMIT:
            problems.append(f"cpu {of_limit:.0f}% of its {cores:g}-core limit")
    row["problems"] = problems
    return row


def _organiser_events():
    path = os.getenv("DEPLOY_HISTORY_FILE")
    if not path or not Path(path).exists():
        return None
    data = json.loads(Path(path).read_text())
    return data if isinstance(data, list) else data.get("events", [])


def _action_log(limit=10):
    if not config.ACTION_LOG.exists():
        return []
    lines = config.ACTION_LOG.read_text(encoding="utf-8").splitlines()[-limit:]
    return [json.loads(line) for line in lines if line.strip()]


def get_deploy_history(lookback="30m", names=None):
    lookback_s = parse_window(lookback, default=1800)
    src = get_source()
    try:
        inspects = src.docker_inspect(names or None)
    except SourceError as e:
        return {"error": str(e)[:300]}
    stats = _stats_by_name()
    now = src.now()
    rows = [describe(i, stats, now, lookback_s) for i in inspects]

    changed = [r for r in rows if r["problems"]]
    changed.sort(key=lambda r: (r["name"] in config.INFRA_CONTAINERS, r["status"] == "running",
                                -len(r["problems"])))
    for r in changed:
        r["evidence"] = registry.add(
            TOOL, "container",
            f"container {r['name']}: {'; '.join(r['problems'])} (status={r['status']}, "
            f"restarts={r['restart_count']}, oom_killed={r['oom_killed']}, health={r['health']}"
            + (f", mem={r.get('mem_used_mb')}/{r.get('mem_limit_mb')}MB" if "mem_used_mb" in r else "")
            + ")", r)
    healthy = sorted(r["name"] for r in rows if not r["problems"])
    organiser = _organiser_events()
    result = {
        "lookback_s": lookback_s, "containers": len(rows), "stats_available": bool(stats),
        "changed_or_unhealthy": changed,
        "healthy": healthy,
        "top_memory": sorted(({"name": r["name"], "mem_pct": r["mem_pct"]}
                              for r in rows if r.get("mem_pct") is not None),
                             key=lambda r: -r["mem_pct"])[:5],
        "organiser_history": organiser if organiser is not None else "not configured",
        "our_actions": _action_log(),
    }
    if not changed:
        result["evidence"] = registry.add(
            TOOL, "container",
            f"all {len(rows)} containers running and healthy; none (re)started in the last "
            f"{lookback_s}s", {"healthy": healthy})
    return cap(result)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("names", nargs="*", help="container names (default: all)")
    p.add_argument("--lookback", default="30m")
    run_cli(get_deploy_history, p, lambda a: {"lookback": a.lookback, "names": a.names})


if __name__ == "__main__":
    main()
