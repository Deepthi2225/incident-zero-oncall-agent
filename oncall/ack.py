"""Acknowledging alerts, and escalating the ones nobody acknowledged.

escalation.json may hold: "acknowledge": {"wait_minutes": 5, "then": ["lead"], "severities": ["SEV1", "SEV2"]}.
`python -m oncall watch` calls escalate_overdue() on every poll. A live incident that alerted someone
and is not acknowledged in time alerts the "then" people, once. Acknowledge with
`python -m oncall ack <incident>`; answering an approval prompt in the terminal also counts.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from oncall import config, notify
from oncall.record import append_line, now_iso

TIMEOUT_EVENT = "ack_timeout"


def _path():
    return config.RUNS_DIR / "acks.jsonl"


def _rows(path):
    rows = []
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def acknowledged():
    return {row.get("incident_id") for row in _rows(_path())}


def acknowledge(incident, by="command line"):
    """Record that a human has this incident. Safe to repeat."""
    if incident and incident not in acknowledged():
        append_line(_path(), {"ts": now_iso(), "incident_id": incident, "by": by})


def waiting(severities=notify.CALL_SEVERITIES):
    """{incident: its first live alert} for incidents that alerted someone and are not acknowledged."""
    done, first = acknowledged(), {}
    rows = [r for r in _rows(notify.notify_log()) if r.get("channel") == "log" and r.get("incident_id")]
    done |= {r["incident_id"] for r in rows if r.get("event") == TIMEOUT_EVENT}
    for row in rows:
        live = not str(row.get("subject") or "").startswith("[TEST]")
        serious = str(row.get("severity") or "").split(" ")[0] in severities
        if live and serious and row["incident_id"] not in done:
            first.setdefault(row["incident_id"], row)
    return first


def _message(incident, row, minutes):
    sev = str(row.get("severity") or "SEV?").split(" ")[0]
    lines = [f"Incident: {incident}", f"Severity: {row.get('severity')}", f"First alert: {row.get('subject')}",
             f"Nobody acknowledged it within {minutes:g} minutes, so it has been escalated to you.",
             f"To acknowledge it: python -m oncall ack {incident}"]
    return {"incident_id": incident, "run_id": row.get("run_id"), "event": TIMEOUT_EVENT, "severity": row.get("severity"),
            "why": f"not acknowledged within {minutes:g} minutes", "lines": lines, "report": row.get("report"),
            "subject": f"[on-call ESCALATION][{sev}] {incident}: nobody acknowledged it",
            "title": "nobody acknowledged this incident"}


def escalate_overdue(log=print, chans=None, now=None):
    """Alert the next people for each overdue incident, once. Returns the incidents escalated."""
    rule = notify._escalation().get("acknowledge")
    if not isinstance(rule, dict) or not rule.get("then"):
        return []
    minutes = float(rule.get("wait_minutes", 5))
    now = now or datetime.now(timezone.utc)
    chans = [c for c in notify.notifiers() if c.channel != "log"] if chans is None else chans
    escalated = []
    for incident, row in waiting(tuple(rule.get("severities") or notify.CALL_SEVERITIES)).items():
        if now - datetime.fromisoformat(row["ts"]) < timedelta(minutes=minutes):
            continue
        message, results = _message(incident, row, minutes), []
        for chan in chans:
            people = notify.contacts_of(rule["then"], chan.contact) if chan.contact else None
            if (people or chan.contact is None) and chan.wants(message):
                if people:
                    chan = notify.copy.copy(chan)
                    chan.use(people)
                results.append(chan.notify(message))
        for result in results:
            if result.get("status") != "sent":
                log(f"[notify] {TIMEOUT_EVENT}: {result.get('error') or 'failed'}")
        if not results or any(r.get("status") == "sent" for r in results):  # all failed: try next poll
            notify.JsonlNotifier().notify(message)
            log(f"[notify] {incident}: not acknowledged in {minutes:g} min, escalated")
            escalated.append(incident)
    return escalated
