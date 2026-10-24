"""Read-only data for the live dashboard. Nothing here changes anything.

Sources: runs/<run_id>.json (incident records), runs/actions.jsonl (the agent's action log),
runs/action_api.jsonl (the Action API's audit log), runs/notifications.jsonl (escalation
notifications), the STOP file, the Action API's GET /health, and the detector's own read-only
Prometheus probe. Every value returned is passed through
record.scrub(): feature-flag data removed, secrets redacted.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

from oncall import config
from oncall.record import load as load_record
from oncall.record import scrub
from oncall.sources.guard import allowed, check

RUN_FILE = re.compile(r"^\d{8}-\d{6}(?:-\d+)?(?:-[a-z]+)?\.json$")
RUN_ID = re.compile(r"^[0-9A-Za-z_-]{1,80}$")
HEALTH_TTL_S = 15
_health_cache = {"at": 0.0, "data": None}


def clean(obj):
    return scrub(json.loads(json.dumps(obj, default=str)))


def run_files():
    files = [p for p in config.RUNS_DIR.glob("*.json") if RUN_FILE.match(p.name)]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def _load(path):
    try:
        return load_record(path)
    except (OSError, ValueError) as e:  # being written or damaged: show it, don't fail
        return {"status": "unreadable", "error": f"{type(e).__name__}", "metadata": {"run_id": path.stem}}


def _primary(record):
    rcs = (record.get("diagnosis") or {}).get("root_causes") or []
    return max(rcs, key=lambda rc: rc.get("confidence", 0)) if rcs else {}


def incidents(limit=50):
    rows = []
    for path in run_files()[:limit]:
        r = _load(path)
        meta, rc = r.get("metadata") or {}, _primary(r)
        rows.append({"run_id": meta.get("run_id") or path.stem, "file": path.name,
                     "time": meta.get("started_at"), "incident_id": meta.get("incident_id"),
                     "mode": meta.get("mode"), "record_status": r.get("status"),
                     "root_cause": rc.get("component"), "fault_class": rc.get("fault_class"),
                     "confidence": (r.get("diagnosis") or {}).get("confidence"),
                     "outcome": (r.get("outcome") or {}).get("status") or r.get("status"),
                     "cost_usd": (r.get("accounting") or {}).get("estimated_cost_usd")})
    return clean({"incidents": rows})


def _find(run_id):
    if not RUN_ID.match(run_id or ""):
        return None
    for path in run_files():
        if path.stem == run_id or path.stem.startswith(run_id + "-"):
            return path
    return None


def incident(run_id=None):
    """The record of run_id, or the newest one. Adds a small "ui" block derived from the record."""
    path = _find(run_id) if run_id else next(iter(run_files()), None)
    if path is None:
        return None
    r = _load(path)
    steps = (r.get("remediation_plan") or {}).get("steps") or []
    awaiting = [s.get("step") for s in steps if (s.get("approval") or {}).get("status") == "awaiting"]
    r["ui"] = {"file": path.name, "awaiting_approval": awaiting if r.get("status") == "running" else [],
               "in_progress": r.get("status") == "running"}
    return clean(r)


def report_text(run_id):
    path = _find(run_id)
    if path is None:
        return None
    report = path.with_suffix(".report.md")
    if not report.exists():
        return "(no report written for this run yet)"
    return scrub(report.read_text(encoding="utf-8", errors="replace"))


def _tail(path, limit):
    path = Path(path)
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            out.append({"unparsed": line[:300]})
    return out


def actions(limit=30):
    api_log = Path(os.getenv("ACTION_API_LOG", config.RUNS_DIR / "action_api.jsonl"))
    return clean({"agent": _tail(config.ACTION_LOG, limit), "action_api": _tail(api_log, limit)})


def notifications(limit=30):
    from oncall.notify import notify_log
    return clean({"notifications": _tail(notify_log(), limit)})


def _action_api_health():
    url = (config.ACTION_API_URL or f"http://127.0.0.1:{os.getenv('ACTION_API_PORT', '8099')}").rstrip("/")
    try:
        check(url)
        resp = requests.get(url + "/health", timeout=1.5)
        return {"url": url, "reachable": resp.ok, "http_status": resp.status_code}
    except Exception as e:  # unreachable is a normal state to display
        return {"url": url, "reachable": False, "error": type(e).__name__}


def status():
    newest = next(iter(run_files()), None)
    watch = "not detectable"
    if newest is not None:
        r = _load(newest)
        if r.get("status") == "running" and time.time() - newest.stat().st_mtime < 120:
            watch = f"investigating ({newest.stem})"
    return clean({"kill_switch": config.KILL_SWITCH_FILE.exists(), "action_api": _action_api_health(),
                  "reviewer": "on" if config.REVIEWER else "off", "agent": watch,
                  "time": time.strftime("%H:%M:%S")})


def _detector():
    if str(config.ROOT) not in sys.path:
        sys.path.insert(0, str(config.ROOT))
    import detector
    return detector


def health(force=False):
    """Every service's rps, error rate and p95 against its detector limits (cached 15 s)."""
    now = time.time()
    if not force and _health_cache["data"] and now - _health_cache["at"] < HEALTH_TTL_S:
        return _health_cache["data"]
    det = _detector()
    try:
        baseline = det.load_baseline()
        breaches, snap = det.poll(baseline)
    except Exception as e:  # Prometheus down: show it, keep serving
        data = clean({"error": f"Prometheus not reachable ({type(e).__name__})", "services": []})
        _health_cache.update(at=now, data=data)
        return data
    breached = {}
    for b in breaches:
        breached.setdefault(b["service"], []).append(b["signal"])
    rows = []
    for svc in sorted(set(snap["rps"]) | set(snap["error_rate"]) | set(snap["p95_ms"]) | set(baseline)):
        if svc in det.EXCLUDE or not allowed(svc):
            continue
        rps, err, p95 = snap["rps"].get(svc, 0.0), snap["error_rate"].get(svc, 0.0), snap["p95_ms"].get(svc)
        limit, quiet = det.p95_limit(svc, baseline), det.silent_threshold(svc, baseline, None)
        if svc in breached:
            state = "red"
        elif (err > det.ERR_RATE_MAX / 2 or (limit and p95 and p95 > 0.7 * limit)
              or (quiet is not None and rps < 1.5 * quiet)):
            state = "amber"
        elif rps < det.MIN_RPS and quiet is None:
            state = "idle"
        else:
            state = "green"
        rows.append({"service": svc, "rps": round(rps, 3), "error_pct": round(err * 100, 2),
                     "p95_ms": None if p95 is None else round(p95, 1), "p95_limit_ms": limit,
                     "silent_below_rps": None if quiet is None else round(quiet, 4),
                     "state": state, "breaches": breached.get(svc, [])})
    data = clean({"services": rows, "breaches": len(breaches), "at": time.strftime("%H:%M:%S")})
    _health_cache.update(at=now, data=data)
    return data
