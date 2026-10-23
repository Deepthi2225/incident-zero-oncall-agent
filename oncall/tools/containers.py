"""get_container_health: state, OOM kills, restarts, health checks, CPU and memory vs limit.

Problem containers are listed first with their evidence; healthy ones are collapsed to names.

CLI: python -m oncall.tools.containers
"""
import argparse
import re

from oncall import config
from oncall.evidence import registry
from oncall.sources import SourceError, get_source
from oncall.tools._common import cap, parse_docker_time, run_cli

TOOL = "get_container_health"
RECENT_START_S = 600
MEM_NEAR_LIMIT = 90.0
CPU_NEAR_LIMIT = 90.0

_SIZE = re.compile(r"([\d.]+)\s*([KMGT]?i?B)", re.I)
_UNIT = {"b": 1, "kb": 1e3, "kib": 1024, "mb": 1e6, "mib": 1024 ** 2, "gb": 1e9, "gib": 1024 ** 3,
         "tb": 1e12, "tib": 1024 ** 4}


def _bytes(text):
    m = _SIZE.search(text or "")
    return float(m.group(1)) * _UNIT.get(m.group(2).lower(), 1) if m else None


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


def _mb(b):
    return round(b / 1024 ** 2, 1) if b is not None else None


def describe(inspect, stats, now):
    st = inspect.get("State", {})
    host = inspect.get("HostConfig", {})
    name = inspect.get("Name", "").lstrip("/")
    started = parse_docker_time(st.get("StartedAt"))
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
        "uptime_s": int(now - started) if started and st.get("Running") else None,
        "cpu_pct": s.get("cpu_pct"),
        "cpu_limit_cores": cores,
        "mem_used_mb": s.get("mem_used_mb"),
        "mem_limit_mb": s.get("mem_limit_mb") if host.get("Memory") else None,
        "mem_pct": s.get("mem_pct") if host.get("Memory") else None,
    }
    problems = []
    if row["status"] != "running":
        problems.append(f"not running ({row['status']}, exit code {row['exit_code']})")
    if row["oom_killed"]:
        problems.append("OOMKilled")
    if row["restart_count"]:
        problems.append(f"restarted {row['restart_count']}x")
    if row["uptime_s"] is not None and row["uptime_s"] < RECENT_START_S:
        problems.append(f"started {row['uptime_s']}s ago")
    if row["health"] in ("unhealthy", "starting"):
        problems.append(f"health {row['health']}")
    if row["mem_pct"] is not None and row["mem_pct"] >= MEM_NEAR_LIMIT:
        problems.append(f"memory {row['mem_pct']}% of limit")
    if row["cpu_pct"] is not None and cores:
        of_limit = row["cpu_pct"] / cores
        row["cpu_pct_of_limit"] = round(of_limit, 1)
        if of_limit >= CPU_NEAR_LIMIT:
            problems.append(f"cpu {of_limit:.0f}% of its {cores:g}-core limit")
    row["problems"] = problems
    return row


def get_container_health(names=None):
    src = get_source()
    try:
        inspects = src.docker_inspect(names or None)
    except SourceError as e:
        return {"error": str(e)[:300]}
    stats = _stats_by_name()
    now = src.now()
    rows = [describe(i, stats, now) for i in inspects]
    bad = [r for r in rows if r["problems"]]
    bad.sort(key=lambda r: (r["name"] in config.INFRA_CONTAINERS, -len(r["problems"])))
    for r in bad:
        r["evidence"] = registry.add(
            TOOL, "container",
            f"container {r['name']}: {'; '.join(r['problems'])} (status={r['status']}, "
            f"restarts={r['restart_count']}, health={r['health']}, mem={r['mem_used_mb']}/"
            f"{r['mem_limit_mb']}MB, cpu={r['cpu_pct']}%)", r)
    healthy = sorted(r["name"] for r in rows if not r["problems"])
    result = {"containers": len(rows), "stats_available": bool(stats), "problems": bad,
              "healthy": healthy}
    if not bad:
        result["evidence"] = registry.add(TOOL, "container",
                                          f"all {len(rows)} containers running and healthy",
                                          {"healthy": healthy})
    # Resource usage of healthy app containers is useful context for leaks / CPU pressure.
    result["top_memory"] = sorted(
        ({"name": r["name"], "mem_pct": r["mem_pct"]} for r in rows if r["mem_pct"] is not None),
        key=lambda r: -r["mem_pct"])[:5]
    return cap(result)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("names", nargs="*", help="container names (default: all)")
    run_cli(get_container_health, p, lambda a: {"names": a.names})


if __name__ == "__main__":
    main()
