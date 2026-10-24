"""Escalation notifications: tell a human when the agent hands an incident over.

Events (one notification per incident per event type, even across agent restarts):
  low_confidence       no root cause reached CONFIDENT (or the investigation ended without one)
  no_allowlisted_fix   a confident root cause, but no Action API action fixes its fault class
  reviewer_objection   the optional reviewer agent lowered confidence or removed steps
  verification_failed  an executed step did not verify (after its rollback, if it had one)
  kill_switch          the kill switch stopped the agent or refused a step
  human_rejected       the on-call engineer said no at the approval prompt
  recovered            optional info message (EMAIL_ON_RECOVERED, default off)

Channels: JsonlNotifier always appends to runs/notifications.jsonl; EmailNotifier sends with the
standard library (smtplib + email.message) when EMAIL_ENABLED=on. Sending runs on background
threads with a deadline, so a slow or broken mail server never blocks or crashes the agent; a
failure is recorded as "email failed: <reason>". Every message is scrubbed (record.scrub): no
feature-flag data, no keys.

The email escalation is based on our teammate's notification work in ocAgent-main.zip, rebuilt
on this repo's design (record, scrubber, fault classes) and limited to email.
"""
import json
import os
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from pathlib import Path

from oncall import config
from oncall.record import append_line, now_iso, record_path, scrub

EVENTS = ["low_confidence", "no_allowlisted_fix", "reviewer_objection", "verification_failed",
          "kill_switch", "human_rejected", "recovered"]
TITLES = {"low_confidence": "low confidence, needs a human", "no_allowlisted_fix": "no allowlisted fix",
          "reviewer_objection": "reviewer objected", "verification_failed": "fix did not verify",
          "kill_switch": "stopped by the kill switch", "human_rejected": "fix rejected by the on-call engineer",
          "recovered": "recovered (info)"}
SEVERITY = {"critical": "SEV1", "high": "SEV2", "medium": "SEV3", "low": "SEV4"}
FAILED_VERIFY = ("not_recovered", "partial", "inconclusive")
MAX_EVIDENCE_LINES = 5


def notify_log():
    return Path(os.getenv("ONCALL_NOTIFY_LOG") or config.RUNS_DIR / "notifications.jsonl")


# --- which events happened ------------------------------------------------------------------------

def events_for(record):
    """[(event, why)] for a finished record, most important first. Derived only from the record."""
    from oncall.agent.diagnosis import CONFIDENT
    inv = (record.get("investigation") or {}).get("outcome")
    diag = record.get("diagnosis") or {}
    plan = record.get("remediation_plan") or {}
    steps = plan.get("steps") or []
    review = record.get("review") or {}
    out = []

    switch = [s for s in steps if (s.get("execution") or {}).get("status") == "refused"
              and "kill switch" in str((s.get("execution") or {}).get("reason", "")).lower()]
    if inv == "killed":
        out.append(("kill_switch", "the kill switch stopped the investigation: "
                                   + str((record.get("investigation") or {}).get("error") or "STOP file present")))
    elif switch:
        out.append(("kill_switch", f"step {switch[0].get('step')} ({switch[0].get('action')} on "
                                   f"{switch[0].get('target')}) was refused: {switch[0]['execution']['reason']}"))

    rejected = [s for s in steps if (s.get("approval") or {}).get("status") == "rejected"]
    if rejected:
        s = rejected[0]
        out.append(("human_rejected", f"step {s.get('step')} ({s.get('action')} on {s.get('target')}) was "
                                      f"rejected: {(s.get('approval') or {}).get('notes') or 'no reason given'}"))

    failed = [s for s in steps if s.get("outcome") in FAILED_VERIFY]
    if failed and plan.get("outcome") in ("stopped", "partial"):
        s = failed[0]
        rb = (s.get("rollback_result") or {}).get("status", "none")
        out.append(("verification_failed", f"step {s.get('step')} ({s.get('action')} on {s.get('target')}) "
                                           f"verified as {s['outcome']}; rollback: {rb}"))

    if review.get("status") == "done" and review.get("escalated"):
        out.append(("reviewer_objection", f"the reviewer said {review.get('verdict')}: confidence "
                                          f"{review.get('confidence_before')} -> {review.get('confidence_after')}"))
    elif inv not in ("submitted", "killed", None):
        out.append(("low_confidence", f"the investigation ended without a diagnosis ({inv})"))
    elif inv == "submitted" and diag.get("status") == "escalate":
        out.append(("low_confidence", f"confidence {diag.get('confidence')} is below {CONFIDENT}"))
    elif inv == "submitted" and any(e.get("source") == "no allowlisted fix" for e in record.get("escalations") or []):
        e = next(e for e in record["escalations"] if e.get("source") == "no allowlisted fix")
        out.append(("no_allowlisted_fix", f"{e.get('target')} [{e.get('fault_class')}]: {e.get('reason')}"))

    if ((record.get("outcome") or {}).get("status") == "recovered" and config.EMAIL_ON_RECOVERED
            and not out):
        out.append(("recovered", (record.get("outcome") or {}).get("summary", "recovered")))
    return out


# --- the message ------------------------------------------------------------------------------------

def _primary(record):
    diag = record.get("diagnosis") or {}
    rcs = diag.get("root_causes") or []
    if rcs:
        return max(rcs, key=lambda rc: rc.get("confidence") or 0)
    origin = diag.get("suspected_origin") or {}
    alert = record.get("alert") or {}
    return {"component": origin.get("service") or alert.get("service")}


def severity(record):
    crit = config.CRITICALITY.get(_primary(record).get("component") or "")
    return f"{SEVERITY[crit]} ({crit} service)" if crit else "unknown (service criticality not known)"


def affected(record):
    diag = record.get("diagnosis") or {}
    alert = record.get("alert") or {}
    names = [a.get("service") for a in diag.get("affected_services") or [] if isinstance(a, dict)]
    names += alert.get("alerting_services") or ([alert["service"]] if alert.get("service") else [])
    return list(dict.fromkeys(n for n in names if n))


def build_event(record, event, why):
    """The notification for one event: a dict with subject and body lines, already scrubbed."""
    meta = record.get("metadata") or {}
    diag = record.get("diagnosis") or {}
    rc = _primary(record)
    incident = meta.get("incident_id") or meta.get("run_id")
    sev = severity(record)
    root = (f"{rc.get('component')} [{rc.get('fault_class')}]: {rc.get('cause')} "
            f"(confidence {rc.get('confidence')})" if rc.get("fault_class") else "not determined with valid evidence")
    cited = [e for e in record.get("evidence") or [] if e.get("cited")] or (record.get("evidence") or [])
    evidence = [f"{e.get('id')} [{e.get('tool')}] {str(e.get('observation', ''))[:200]}"
                for e in cited[:MAX_EVIDENCE_LINES]]
    next_steps = []
    for e in record.get("escalations") or []:
        next_steps += e.get("next_steps") or []
    if event == "kill_switch":
        next_steps.append("Turn the kill switch off (python -m oncall resume) only when it is safe to act again.")
    if event == "verification_failed":
        next_steps.append("Check the rollback result and the service's health before trying another fix.")
    next_steps = list(dict.fromkeys(next_steps)) or ["Read the report and decide on a fix by hand."]

    lines = [f"Incident: {incident} (run {meta.get('run_id')})",
             f"Severity: {sev}",
             f"Event: {TITLES[event]}",
             f"Why: {why}",
             f"Affected services: {', '.join(affected(record)) or 'not recorded'}",
             f"Root cause: {root}",
             f"Diagnosis confidence: {diag.get('confidence', 'none')}",
             "", "Top evidence:"] + [f"  - {x}" for x in evidence or ["none recorded"]] + \
            ["", "Open questions:"] + [f"  - {q}" for q in diag.get("open_questions") or ["none"]] + \
            ["", "Next steps:"] + [f"  - {s}" for s in next_steps] + \
            ["", f"Report: {(record.get('report_paths') or {}).get('report') or 'not written'}",
             f"Outcome so far: {(record.get('outcome') or {}).get('summary', 'not recorded')}",
             "", "Sent by the AI on-call agent. Nothing was changed because of this message."]
    subject = f"[on-call {sev.split(' ')[0]}] {incident}: {TITLES[event]}"
    clean = scrub({"subject": subject, "lines": lines})
    return {"incident_id": incident, "run_id": meta.get("run_id"), "event": event, "severity": sev,
            "why": why, "subject": " ".join(str(clean["subject"]).split()), "lines": clean["lines"],
            "report": (record.get("report_paths") or {}).get("report")}


# --- channels ----------------------------------------------------------------------------------------

class Notifier:
    """notify(event) -> {"channel", "recipients", "status", "error"?}. Must not raise."""
    channel = "none"

    def notify(self, event):
        raise NotImplementedError


class JsonlNotifier(Notifier):
    """Always on: one line per event in runs/notifications.jsonl (local file, fast)."""
    channel = "log"

    def __init__(self, path=None):
        self.path = path or notify_log()

    def notify(self, event):
        try:
            append_line(self.path, {"ts": now_iso(), "channel": self.channel, "status": "logged",
                                    **{k: event[k] for k in ("incident_id", "run_id", "event", "severity",
                                                             "why", "subject", "report")}})
            return {"channel": self.channel, "recipients": [str(self.path)], "status": "logged"}
        except Exception as e:
            return {"channel": self.channel, "recipients": [], "status": "failed",
                    "error": f"log failed: {type(e).__name__}: {e}"}


class EmailNotifier(Notifier):
    channel = "email"

    def __init__(self):
        self.host, self.port = config.SMTP_HOST, config.SMTP_PORT
        self.user, self.password = config.SMTP_USER, config.SMTP_PASSWORD
        self.starttls, self.timeout = config.SMTP_STARTTLS, config.EMAIL_TIMEOUT_S
        self.sender = config.EMAIL_FROM or config.SMTP_USER or "oncall-agent@localhost"
        self.to = list(config.EMAIL_TO)

    def message(self, event):
        msg = EmailMessage()
        msg["Subject"] = event["subject"]
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.to)
        msg.set_content("\n".join(event["lines"]) + "\n")
        return msg

    def notify(self, event):
        result = {"channel": self.channel, "recipients": self.to}
        if not self.host or not self.to:
            return {**result, "status": "failed", "error": "email failed: SMTP_HOST or EMAIL_TO is not set"}
        try:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
                if self.starttls:
                    smtp.starttls(context=ssl.create_default_context())
                if self.user:
                    smtp.login(self.user, self.password)
                smtp.send_message(self.message(event))
            return {**result, "status": "sent"}
        except Exception as e:  # any failure is recorded, never raised
            return {**result, "status": "failed", "error": f"email failed: {type(e).__name__}: {e}"[:300]}


def notifiers():
    chans = [JsonlNotifier()]
    if config.EMAIL_ENABLED:
        chans.append(EmailNotifier())
    return chans


# --- dispatch ----------------------------------------------------------------------------------------

def already_sent(record):
    """{event} already notified for this incident: in this record, or in an earlier run's record of
    the same incident (an agent restart re-investigating it)."""
    done = {n.get("event") for n in record.get("notifications") or []}
    incident = (record.get("metadata") or {}).get("incident_id")
    index = config.RUNS_DIR / "index.jsonl"
    if not incident or not index.exists():
        return done
    for line in index.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
            if row.get("incident_id") != incident or row.get("run_id") == record["metadata"].get("run_id"):
                continue
            other = json.loads(record_path(row["run_id"], row.get("mode") or "live").read_text(encoding="utf-8"))
            done |= {n.get("event") for n in other.get("notifications") or []}
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return done


def _run(chan, message, box):
    try:
        box.update(chan.notify(message))
    except Exception as e:  # a channel must never take the agent down
        box.update(channel=chan.channel, status="failed", error=f"{chan.channel} failed: {type(e).__name__}: {e}"[:300])


def dispatch(record, chans=None, log=print):
    """Notify every new event of a finished record. Returns the notification entries to add to the
    record. Each channel runs on its own daemon thread; all share one deadline (EMAIL_TIMEOUT_S plus
    a small margin), so this returns in bounded time even if a mail server hangs."""
    chans = notifiers() if chans is None else chans
    sent = already_sent(record)
    todo = [(e, why) for e, why in events_for(record) if e not in sent]
    jobs = []
    for event, why in todo:
        message = build_event(record, event, why)
        for chan in chans:
            box = {}
            t = threading.Thread(target=_run, args=(chan, message, box), daemon=True)
            t.start()
            jobs.append((event, chan, t, box))
    deadline = time.monotonic() + config.EMAIL_TIMEOUT_S + 2
    entries = []
    for event, chan, t, box in jobs:
        t.join(max(0.0, deadline - time.monotonic()))
        result = dict(box) if not t.is_alive() else {
            "channel": chan.channel, "recipients": getattr(chan, "to", []), "status": "failed",
            "error": f"email failed: no answer within {config.EMAIL_TIMEOUT_S:.0f}s"}
        entry = {"at": now_iso(), "event": event, "channel": result.get("channel", chan.channel),
                 "recipients": list(result.get("recipients") or []), "status": result.get("status", "failed")}
        if result.get("error"):
            entry["error"] = result["error"]
        entries.append(scrub(entry))
        if entry["channel"] != "log":  # the email outcome goes to the log file too
            append_line(notify_log(), {"ts": entry["at"], "incident_id": (record.get("metadata") or {}).get("incident_id"),
                                       "run_id": (record.get("metadata") or {}).get("run_id"), **entry})
        log(f"[notify] {event} via {entry['channel']}: {entry['status']}"
            + (f" ({entry['error']})" if entry.get("error") else ""))
    return entries
