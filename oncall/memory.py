"""What P0ppy remembers: earlier live incidents on the services alerting now. Read-only.

A hint for where to look first, never evidence: the diagnosis can only cite this run's own evidence.
"""
import json

from oncall import config
from oncall.agent.actions import depends_on


def similar(alert, limit=3):
    """Newest first: past live incidents whose root cause was an alerting service or one it depends on."""
    services = alert.get("alerting_services") or ([alert["service"]] if alert.get("service") else [])
    try:
        lines = (config.RUNS_DIR / "index.jsonl").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    found = []
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        component = row.get("component") if isinstance(row, dict) else None
        if component and row.get("mode") == "live" and any(s == component or depends_on(s, component) for s in services):
            found.append(row)
            if len(found) == limit:
                break
    return found


def note(alert):
    """Text added to the alert for the model, or "" when nothing similar has happened."""
    past = similar(alert)
    if not past:
        return ""
    rows = "\n".join(f"- {r.get('time')}: {r.get('component')} [{r.get('fault_class')}], confidence "
                     f"{r.get('confidence')}, ended {r.get('status')}" for r in past)
    return ("\n\nEarlier incidents on these services (a hint for where to look first, NOT evidence; this one "
            f"may be different, so confirm everything from this incident's own telemetry):\n{rows}")
