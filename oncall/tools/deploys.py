"""get_deploy_history: what changed recently?

Sources, in order: an organiser-provided history file (DEPLOY_HISTORY_FILE, format TBD),
container create/start times and images from docker, and our own Action API log.

CLI: python -m oncall.tools.deploys --lookback 1h
"""
import argparse
import json
import os
from pathlib import Path

from oncall import config
from oncall.evidence import registry
from oncall.sources import SourceError, get_source
from oncall.tools._common import cap, parse_docker_time, parse_window, run_cli

TOOL = "get_deploy_history"


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


def get_deploy_history(lookback="1h"):
    lookback_s = parse_window(lookback, default=3600)
    src = get_source()
    now = src.now()
    changes = []
    try:
        inspects = src.docker_inspect()
    except SourceError as e:
        inspects = []
        changes.append({"error": str(e)[:200]})
    for i in inspects:
        name = i.get("Name", "").lstrip("/")
        for event, ts in (("created", i.get("Created")), ("started", i.get("State", {}).get("StartedAt"))):
            t = parse_docker_time(ts)
            if t and now - t <= lookback_s:
                changes.append({"container": name, "event": event, "at": ts,
                                "ago_s": int(now - t), "image": i.get("Config", {}).get("Image"),
                                "restart_count": i.get("RestartCount", 0)})
    changes.sort(key=lambda c: c.get("ago_s", 0))
    for c in changes[:8]:
        if "container" in c:
            c["evidence"] = registry.add(
                TOOL, "deploy",
                f"container {c['container']} {c['event']} {c['ago_s']}s ago (image {c['image']}, "
                f"restarts {c['restart_count']})", c)

    organiser = _organiser_events()
    actions = _action_log()
    result = {"lookback_s": lookback_s, "container_changes": changes[:12],
              "organiser_history": organiser if organiser is not None else "not configured",
              "our_actions": actions}
    if not [c for c in changes if "container" in c]:
        result["evidence"] = registry.add(
            TOOL, "deploy", f"no container was created or (re)started in the last {lookback_s}s",
            {"lookback_s": lookback_s})
    return cap(result)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--lookback", default="1h")
    run_cli(get_deploy_history, p, lambda a: {"lookback": a.lookback})


if __name__ == "__main__":
    main()
