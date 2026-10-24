"""Escalation notifications: tell a human when the agent hands an incident over.

Events (one notification per incident, event type and channel, even across agent restarts; a
failed send is tried again by the next run of the same incident):
  low_confidence       no root cause reached CONFIDENT (or the investigation ended without one)
  no_allowlisted_fix   a confident root cause, but no Action API action fixes its fault class
  reviewer_objection   the optional reviewer agent lowered confidence or removed steps
  approval_needed      a step was not run because nobody approved it (no terminal, --non-interactive)
  verification_failed  an executed step (or a manual fix) did not verify, after rollback if any
  kill_switch          the kill switch stopped the agent or refused a step
  human_rejected       the on-call engineer said no at the approval prompt
  recovered            optional info message (EMAIL_ON_RECOVERED, default off)

Channels: JsonlNotifier always appends to runs/notifications.jsonl; EmailNotifier sends with the
standard library (smtplib + email.message) when EMAIL_ENABLED=on, to exactly SMTP_HOST:SMTP_PORT.
The whole SMTP conversation runs under one deadline (EMAIL_TIMEOUT_S) on a worker thread that
dispatch() waits for, so a send is never cut off by the process exiting and a hung server never
blocks the agent for longer than the deadline. Every attempt prints one line ("email sent to ..."
or "email failed: <reason>") and is recorded in notifications.jsonl and the incident record.
Every message is scrubbed (record.scrub): no feature-flag data, no keys.

The email escalation is based on our teammate's notification work in ocAgent-main.zip, rebuilt
on this repo's design (record, scrubber, fault classes) and limited to email.
"""
import json
import os
import smtplib
import socket
import ssl
import threading
import time
from email.message import EmailMessage
from pathlib import Path

from oncall import config
from oncall.record import append_line, now_iso, record_path, scrub

EVENTS = ["low_confidence", "no_allowlisted_fix", "reviewer_objection", "approval_needed",
          "verification_failed", "kill_switch", "human_rejected", "recovered"]
TITLES = {"low_confidence": "low confidence, needs a human", "no_allowlisted_fix": "no allowlisted fix",
          "reviewer_objection": "reviewer objected", "approval_needed": "fix waiting for a human approval",
          "verification_failed": "fix did not verify", "kill_switch": "stopped by the kill switch",
          "human_rejected": "fix rejected by the on-call engineer", "recovered": "recovered (info)"}
SEVERITY = {"critical": "SEV1", "high": "SEV2", "medium": "SEV3", "low": "SEV4"}
FAILED_VERIFY = ("not_recovered", "partial", "inconclusive")
DONE = ("sent", "logged")
MAX_EVIDENCE_LINES = 5


def notify_log():
    return Path(os.getenv("ONCALL_NOTIFY_LOG") or config.RUNS_DIR / "notifications.jsonl")


# --- which events happened ------------------------------------------------------------------------

def _step(s):
    return f"step {s.get('step')} ({s.get('action')} on {s.get('target')})"


def events_for(record):
    """[(event, why)] for a finished record, most important first. Derived only from the record."""
    from oncall.agent.diagnosis import CONFIDENT
    inv = (record.get("investigation") or {}).get("outcome")
    diag = record.get("diagnosis") or {}
    plan = record.get("remediation_plan") or {}
    steps = plan.get("steps") or []
    review = record.get("review") or {}
    manual = record.get("manual_verifications") or []
    out = []

    def refused(s):
        return (s.get("execution") or {}).get("status") == "refused"

    switch = [s for s in steps if refused(s) and "kill switch" in str(s["execution"].get("reason", "")).lower()]
    if inv == "killed":
        out.append(("kill_switch", "the kill switch stopped the investigation: "
                                   + str((record.get("investigation") or {}).get("error") or "STOP file present")))
    elif switch:
        out.append(("kill_switch", f"{_step(switch[0])} was refused: {switch[0]['execution']['reason']}"))

    rejected = [s for s in steps if (s.get("approval") or {}).get("status") == "rejected"]
    if rejected:
        out.append(("human_rejected", f"{_step(rejected[0])} was rejected: "
                                      f"{(rejected[0].get('approval') or {}).get('notes') or 'no reason given'}"))

    waiting = [s for s in steps if refused(s) and (s.get("approval") or {}).get("status") in ("pending", "awaiting")]
    if waiting and not switch:
        out.append(("approval_needed", f"{_step(waiting[0])} was not run: nobody approved it "
                                       f"({(waiting[0].get('approval') or {}).get('notes') or 'no terminal'})"))

    failed = [s for s in steps if s.get("outcome") in FAILED_VERIFY]
    if manual and manual[-1].get("outcome") in FAILED_VERIFY:
        out.append(("verification_failed", f"the manual fix verified as {manual[-1]['outcome']}"))
    elif failed and plan.get("outcome") in ("stopped", "partial"):
        s = failed[0]
        rb = (s.get("rollback_result") or {}).get("status", "none")
        out.append(("verification_failed", f"{_step(s)} verified as {s['outcome']}; rollback: {rb}"))

    if review.get("status") == "done" and review.get("escalated"):
        out.append(("reviewer_objection", f"the reviewer said {review.get('verdict')}: confidence "
                                          f"{review.get('confidence_before')} -> {review.get('confidence_after')}"))
    elif inv not in ("submitted", "killed", None):
        out.append(("low_confidence", f"the investigation ended without a diagnosis ({inv})"))
    elif inv == "submitted" and diag.get("status") == "escalate":
        out.append(("low_confidence", f"confidence {diag.get('confidence')} is below {CONFIDENT}"))
    elif inv == "submitted" and any(e.get("source") == "no allowlisted fix" for e in record.get("escalations") or []):
        e = next(e for e in record["escalations"] if e.get("source") == "no allowlisted fix")
        head = f"{e.get('target')} [{e.get('fault_class')}]"
        reason = str(e.get("reason") or "")
        out.append(("no_allowlisted_fix", reason if reason.startswith(head) else f"{head}: {reason}"))

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
    if event == "approval_needed":
        next_steps.append("Review the proposed step in the report; run the agent in a terminal to approve it, "
                          "or fix by hand and run python -m oncall verify <run_id>.")
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
                                    **{k: event.get(k) for k in ("incident_id", "run_id", "event", "severity",
                                                                 "why", "subject", "report")}})
            return {"channel": self.channel, "recipients": [str(self.path)], "status": "logged"}
        except Exception as e:
            return {"channel": self.channel, "recipients": [], "status": "failed",
                    "error": f"log failed: {type(e).__name__}: {e}"}


def _explain(e, host, port):
    """The exception as one line, with a hint for the usual local-testing mistakes."""
    text = f"{type(e).__name__}: {e}"
    if isinstance(e, ConnectionRefusedError):
        text += f" (nothing is listening on {host}:{port})"
    elif isinstance(e, socket.gaierror):
        text += f" (cannot resolve SMTP_HOST {host!r})"
    elif isinstance(e, smtplib.SMTPNotSupportedError) and "STARTTLS" in str(e).upper():
        text += " (the server has no STARTTLS; set SMTP_STARTTLS=off for a local test server)"
    elif isinstance(e, (TimeoutError, socket.timeout)):
        text += f" (no answer from {host}:{port} within EMAIL_TIMEOUT_S)"
    return text


class EmailNotifier(Notifier):
    channel = "email"

    def __init__(self):
        self.host, self.port = config.SMTP_HOST, config.SMTP_PORT  # used exactly as given
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

    def send(self, msg):
        """One SMTP conversation, all of it within self.timeout seconds."""
        deadline = time.monotonic() + self.timeout

        def left():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"the SMTP conversation took longer than {self.timeout:g}s")
            return remaining

        smtp = smtplib.SMTP(timeout=left())
        try:
            smtp.connect(self.host, self.port)
            smtp.sock.settimeout(left())
            smtp.ehlo()
            if self.starttls:  # SMTP_STARTTLS=off skips it entirely
                smtp.starttls(context=ssl.create_default_context())
                smtp.sock.settimeout(left())
                smtp.ehlo()
            if self.user:  # no login without a user
                smtp.login(self.user, self.password)
            smtp.sock.settimeout(left())
            smtp.send_message(msg)
            try:
                smtp.quit()
            except smtplib.SMTPException:  # the message is already accepted
                pass
        finally:
            smtp.close()

    def notify(self, event):
        result = {"channel": self.channel, "recipients": self.to}
        missing = [n for n, v in (("SMTP_HOST", self.host), ("EMAIL_TO", self.to)) if not v]
        if missing:
            return {**result, "status": "failed", "error": f"email failed: {' and '.join(missing)} not set"}
        try:
            self.send(self.message(event))
            return {**result, "status": "sent"}
        except Exception as e:  # any failure is recorded, never raised
            return {**result, "status": "failed", "error": f"email failed: {_explain(e, self.host, self.port)}"[:300]}


def notifiers():
    chans = [JsonlNotifier()]
    if config.EMAIL_ENABLED:
        chans.append(EmailNotifier())
    return chans


# --- dispatch ----------------------------------------------------------------------------------------

def already_done(record):
    """{(event, channel)} already notified successfully for this incident: in this record, or in an
    earlier run's record of the same incident (an agent restart re-investigating it). Failed
    attempts do not count, so the next run tries again."""
    def ok(notes):
        return {(n.get("event"), n.get("channel")) for n in notes or [] if n.get("status") in DONE}
    done = ok(record.get("notifications"))
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
            done |= ok(other.get("notifications"))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return done


def _run(chan, message, box):
    try:
        box.update(chan.notify(message))
    except Exception as e:  # a channel must never take the agent down
        box.update(channel=chan.channel, status="failed", error=f"{chan.channel} failed: {type(e).__name__}: {e}"[:300])


def _line(event, entry):
    """The one terminal line for one attempt."""
    if entry["channel"] == "email":
        if entry["status"] == "sent":
            return f"[notify] {event}: email sent to {', '.join(entry['recipients'])}"
        return f"[notify] {event}: {entry.get('error') or 'email failed: unknown reason'}"
    note = "" if config.EMAIL_ENABLED else " (email off: EMAIL_ENABLED is not on)"
    return f"[notify] {event}: {entry['status']} to {notify_log()}{note}"


def dispatch(record, chans=None, log=print):
    """Notify every new event of a finished record; returns the entries to add to the record.
    Channels run on worker threads and dispatch waits for them: each send is bounded by
    EMAIL_TIMEOUT_S, so this returns in bounded time with every attempt's final status."""
    chans = notifiers() if chans is None else chans
    done = already_done(record)
    meta = record.get("metadata") or {}
    jobs = []
    for event, why in events_for(record):
        todo = [c for c in chans if (event, c.channel) not in done]
        if not todo:
            log(f"[notify] {event}: already notified for {meta.get('incident_id')}, not repeated")
            continue
        message = build_event(record, event, why)
        for chan in todo:
            box = {}
            t = threading.Thread(target=_run, args=(chan, message, box), name=f"notify-{event}-{chan.channel}")
            t.start()
            jobs.append((event, chan, t, box))
    deadline = time.monotonic() + config.EMAIL_TIMEOUT_S + 2  # the send's own deadline plus a margin
    entries = []
    for event, chan, t, box in jobs:
        t.join(max(0.0, deadline - time.monotonic()))
        result = dict(box) if not t.is_alive() else {
            "channel": chan.channel, "recipients": getattr(chan, "to", []), "status": "failed",
            "error": f"email failed: no answer within {config.EMAIL_TIMEOUT_S:g}s"}
        entry = {"at": now_iso(), "event": event, "channel": result.get("channel", chan.channel),
                 "recipients": list(result.get("recipients") or []), "status": result.get("status", "failed")}
        if result.get("error"):
            entry["error"] = result["error"]
        entry = scrub(entry)
        entries.append(entry)
        if entry["channel"] != "log":  # the email outcome goes to the log file too
            append_line(notify_log(), {"ts": entry["at"], "incident_id": meta.get("incident_id"),
                                       "run_id": meta.get("run_id"), **entry})
        log(_line(event, entry))
    return entries


# --- python -m oncall emailtest ---------------------------------------------------------------------

def settings_lines():
    """The email settings the app loaded; the password only as present/absent."""
    return [f"EMAIL_ENABLED={'on' if config.EMAIL_ENABLED else 'off'}",
            f"SMTP_HOST={config.SMTP_HOST or '(not set)'}", f"SMTP_PORT={config.SMTP_PORT}",
            f"SMTP_USER={config.SMTP_USER or '(not set: no login)'}",
            f"SMTP_PASSWORD={'present' if config.SMTP_PASSWORD else 'absent'}",
            f"SMTP_STARTTLS={'on' if config.SMTP_STARTTLS else 'off'}",
            f"EMAIL_FROM={config.EMAIL_FROM or '(not set)'}", f"EMAIL_TO={', '.join(config.EMAIL_TO) or '(not set)'}",
            f"EMAIL_TIMEOUT_S={config.EMAIL_TIMEOUT_S:g}"]


def send_test():
    """One clearly labelled test email with the app's own settings. Never runs the agent and holds
    no incident data. Returns the notification entry (also logged to notifications.jsonl)."""
    if not config.EMAIL_ENABLED:
        entry = {"channel": "email", "recipients": list(config.EMAIL_TO), "status": "failed",
                 "error": "email failed: EMAIL_ENABLED is off (set EMAIL_ENABLED=on in .env)"}
    else:
        stamp = now_iso()
        message = {"subject": f"[on-call TEST] email test from the AI on-call agent ({stamp})",
                   "lines": ["This is a TEST email from `python -m oncall emailtest`.",
                             "It is not an incident. Nothing was investigated or changed.", "",
                             f"Sent at {stamp} via {config.SMTP_HOST}:{config.SMTP_PORT}."]}
        box = {}
        t = threading.Thread(target=_run, args=(EmailNotifier(), message, box), name="notify-emailtest")
        t.start()
        t.join(config.EMAIL_TIMEOUT_S + 2)
        entry = dict(box) if not t.is_alive() else {
            "channel": "email", "recipients": list(config.EMAIL_TO), "status": "failed",
            "error": f"email failed: no answer within {config.EMAIL_TIMEOUT_S:g}s"}
    entry = scrub({"at": now_iso(), "event": "email_test", **entry})
    append_line(notify_log(), {"ts": entry["at"], "incident_id": None, "run_id": None, **entry})
    return entry
