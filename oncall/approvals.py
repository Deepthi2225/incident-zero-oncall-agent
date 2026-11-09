"""Approval decisions from the dashboard, handed to the waiting agent through a file.

A decision names the run, step, action and target it was made for, and the agent ignores one that
does not match the step it is waiting on. The first decision for a step wins.
"""
import json

from oncall import config
from oncall.record import now_iso


def _path(run_id, number):
    return config.RUNS_DIR / "approvals" / f"{run_id}-step{number}.json"


def submit(run_id, step, decision, by, note=""):
    """Record a human's "approved" or "rejected". False if this step already has a decision."""
    if decision not in ("approved", "rejected"):
        raise ValueError(f"unknown decision {decision!r}")
    path = _path(run_id, step["step"])
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "x", encoding="utf-8") as f:
            json.dump({"decision": decision, "by": by, "at": now_iso(), "note": note,
                       "action": step.get("action"), "target": step.get("target")}, f)
    except FileExistsError:
        return False
    return True


def take(run_id, step):
    """The decision waiting for this exact step, removed so it is used once, or None."""
    path = _path(run_id, step["step"])
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # none yet, or still being written
        return None
    path.unlink(missing_ok=True)
    if (found.get("action"), found.get("target")) != (step.get("action"), step.get("target")):
        return None  # made for a different step: never applied to this one
    return found if found.get("decision") in ("approved", "rejected") else None
