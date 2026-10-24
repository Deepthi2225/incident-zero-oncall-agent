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
  approval_page        sent DURING the run, the moment a step's approval box is shown in the
                       terminal (page(); one per step, EMAIL_ON_APPROVAL, default on)

Channels: JsonlNotifier always appends to runs/notifications.jsonl. When EMAIL_ENABLED=on, email
goes out by EMAIL_TRANSPORT: smtp (default; EmailNotifier, standard library smtplib +
email.message, to exactly SMTP_HOST:SMTP_PORT) or resend (ResendNotifier, one HTTPS POST to
Resend's API with RESEND_API_KEY; the key is never printed or logged). Both use the same deadline.
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


def body_text(event):
    """The email body: an optional greeting (ONCALL_PRIMARY_NAME) and the event's lines."""
    greeting = [f"Hello {config.ONCALL_PRIMARY_NAME},", ""] if config.ONCALL_PRIMARY_NAME else []
    return "\n".join(greeting + list(event["lines"])) + "\n"


class EmailNotifier(Notifier):
    """SMTP transport (EMAIL_TRANSPORT=smtp, the default)."""
    channel, transport = "email", "smtp"

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
        msg.set_content(body_text(event))
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
        result = {"channel": self.channel, "transport": self.transport, "recipients": self.to}
        missing = [n for n, v in (("SMTP_HOST", self.host), ("EMAIL_TO", self.to)) if not v]
        if missing:
            return {**result, "status": "failed", "error": f"email failed: {' and '.join(missing)} not set"}
        try:
            self.send(self.message(event))
            return {**result, "status": "sent"}
        except Exception as e:  # any failure is recorded, never raised
            return {**result, "status": "failed", "error": f"email failed: {_explain(e, self.host, self.port)}"[:300]}


class ResendNotifier(Notifier):
    """Resend transport (EMAIL_TRANSPORT=resend): one HTTPS POST to Resend's API
    (https://resend.com/docs/api-reference/emails/send-email). The API key goes only into the
    Authorization header; it is never printed, logged or stored, and the scrubber redacts it."""
    channel, transport = "email", "resend"

    def __init__(self, http=None):
        import requests
        self.http = http or requests  # tests pass a fake client
        self.url, self.key, self.timeout = config.RESEND_URL, config.RESEND_API_KEY, config.EMAIL_TIMEOUT_S
        self.sender = config.EMAIL_FROM or config.RESEND_DEFAULT_FROM
        self.to = list(config.EMAIL_TO)

    def request(self, event):
        """(headers, json) of the API call."""
        return ({"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
                {"from": self.sender, "to": self.to, "subject": event["subject"], "text": body_text(event)})

    def notify(self, event):
        result = {"channel": self.channel, "transport": self.transport, "recipients": self.to}
        missing = [n for n, v in (("RESEND_API_KEY", self.key), ("EMAIL_TO", self.to)) if not v]
        if missing:
            return {**result, "status": "failed", "error": f"email failed: {' and '.join(missing)} not set"}
        headers, body = self.request(event)
        try:
            resp = self.http.post(self.url, headers=headers, json=body, timeout=self.timeout)
        except Exception as e:  # network errors: the message never names the key
            return {**result, "status": "failed", "error": f"email failed: Resend {type(e).__name__}: {e}"[:300]}
        try:
            data = resp.json()
        except ValueError:
            data = {}
        data = data if isinstance(data, dict) else {}
        if 200 <= resp.status_code < 300 and data.get("id"):
            return {**result, "status": "sent", "message_id": str(data["id"])}
        reason = data.get("message") or data.get("name") or (getattr(resp, "text", "") or "")[:200] or "no details"
        hint = (" (Resend's onboarding@resend.dev sender only delivers to the Resend account owner's own "
                "address until a domain is verified)" if resp.status_code == 403 else "")
        return {**result, "status": "failed", "error": f"email failed: Resend HTTP {resp.status_code}: {reason}{hint}"[:300]}


def email_notifier():
    """The email transport chosen by EMAIL_TRANSPORT (smtp or resend)."""
    if config.EMAIL_TRANSPORT == "resend":
        return ResendNotifier()
    if config.EMAIL_TRANSPORT != "smtp":
        return _Unknown()
    return EmailNotifier()


class _Unknown(Notifier):
    channel = "email"

    def notify(self, event):
        return {"channel": "email", "recipients": list(config.EMAIL_TO), "status": "failed",
                "error": f"email failed: EMAIL_TRANSPORT={config.EMAIL_TRANSPORT!r} is not smtp or resend"}


def notifiers():
    chans = [JsonlNotifier()]
    if config.EMAIL_ENABLED:
        chans.append(email_notifier())
    return chans


# --- dispatch ----------------------------------------------------------------------------------------

def already_done(record):
    """{(event, channel, key)} already notified successfully for this incident: in this record, or
    in an earlier run's record of the same incident (an agent restart re-investigating it). key is
    None except for per-step pages. Failed attempts do not count, so the next run tries again."""
    def ok(notes):
        return {(n.get("event"), n.get("channel"), n.get("key")) for n in notes or [] if n.get("status") in DONE}
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
            via = f" (resend id {entry['message_id']})" if entry.get("message_id") else ""
            return f"[notify] {event}: email sent{via} to {', '.join(entry['recipients'])}"
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
        todo = [c for c in chans if (event, c.channel, None) not in done]
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
        entry = _finish(event, chan, t, box, meta)
        entries.append(entry)
        log(_line(event, entry))
    return entries


def _finish(event, chan, thread, box, meta, key=None):
    """The record entry for one channel's attempt (the thread has ended or ran out of time); an
    email outcome is also appended to notifications.jsonl."""
    result = dict(box) if not thread.is_alive() else {
        "channel": chan.channel, "recipients": getattr(chan, "to", []), "status": "failed",
        "error": f"email failed: no answer within {config.EMAIL_TIMEOUT_S:g}s"}
    entry = {"at": now_iso(), "event": event, "channel": result.get("channel", chan.channel),
             "recipients": list(result.get("recipients") or []), "status": result.get("status", "failed")}
    for name in ("transport", "message_id", "error"):
        if result.get(name):
            entry[name] = result[name]
    if key:
        entry["key"] = key
    entry = scrub(entry)
    if entry["channel"] != "log":
        append_line(notify_log(), {"ts": entry["at"], "incident_id": meta.get("incident_id"),
                                   "run_id": meta.get("run_id"), **entry})
    return entry


# --- the "approval needed" page --------------------------------------------------------------------

PAGE = "approval_page"


def page_key(step):
    return f"step{step.get('step')}:{step.get('action')}:{step.get('target')}"


def build_page(record, step):
    """The page for one step whose approval box is being shown: subject and body lines, scrubbed."""
    meta, diag = record.get("metadata") or {}, record.get("diagnosis") or {}
    service = step.get("component") or step.get("target")
    crit = config.CRITICALITY.get(service or "")
    sev = SEVERITY.get(crit, "SEV?")
    incident = meta.get("incident_id") or meta.get("run_id")
    rc = next((r for r in diag.get("root_causes") or [] if r.get("component") == service), None) or _primary(record)
    review = record.get("review") or {}
    if not review:
        reviewer = ["Reviewer: did not run"]
    elif review.get("status") == "skipped":
        reviewer = [f"Reviewer: skipped ({review.get('reason')})"]
    else:
        reviewer = [f"Reviewer: {review.get('verdict')} (confidence {review.get('confidence_before')} -> "
                    f"{review.get('confidence_after')})"]
        reviewer += [f"  objection: {o.get('text')} ({', '.join(o.get('evidence') or [])})"
                     for o in review.get("objections") or []]
    evidence = {e.get("id"): e for e in record.get("evidence") or []}
    ids = [i for i in (rc or {}).get("evidence") or [] if i in evidence] or \
          [e.get("id") for e in record.get("evidence") or [] if e.get("cited")]
    top = [f"  - {i} [{evidence[i].get('tool')}] {str(evidence[i].get('observation', ''))[:200]}"
           for i in ids[:MAX_EVIDENCE_LINES]] or ["  - none recorded"]
    rb = step.get("rollback")
    lines = ["This is an on-call page from Incident Zero. A fix is waiting for your approval in the terminal.",
             "",
             f"Incident: {incident} (run {meta.get('run_id')})",
             f"Severity: {sev}" + (f" ({crit} service)" if crit else " (service criticality not known)"),
             f"Root cause: {rc.get('component')} [{rc.get('fault_class')}]: {rc.get('cause')}" if rc and rc.get("fault_class")
             else "Root cause: not determined with valid evidence",
             f"Confidence: {(rc or {}).get('confidence', diag.get('confidence'))} (diagnosis {diag.get('confidence')})",
             *reviewer,
             "",
             f"Proposed action: {step.get('action')} on {step.get('target')}"
             + (f" (flag {step['flag']})" if step.get("flag") else "") + f" (step {step.get('step')})",
             f"Reason: {step.get('reason') or 'not recorded'}",
             f"Expected outcome: {step.get('expected_outcome') or 'not recorded'}",
             f"Risk: {(step.get('risk_assessment') or '').strip() or 'not assessed'}",
             f"Rollback: {rb['action']} ({rb.get('note')})" if rb else "Rollback: none (cannot be undone)",
             "", "Top evidence:", *top, "",
             "Where to approve: in the terminal running the agent (answer y or N at the prompt).",
             f"The read-only dashboard at {config.UI_URL} shows the waiting step.",
             "Approval is NOT possible by replying to this email. Replies are not read.",
             "", f"Report (when the run ends): {(record.get('report_paths') or {}).get('report') or 'not written'}"]
    subject = (f"[ON-CALL PAGE][{sev}] {incident} {service}: approval needed — "
               f"{step.get('action')} on {step.get('target')}")
    clean = scrub({"subject": subject, "lines": lines})
    return {"incident_id": incident, "run_id": meta.get("run_id"), "event": PAGE, "severity": sev,
            "why": f"step {step.get('step')} awaiting approval", "subject": " ".join(str(clean["subject"]).split()),
            "lines": clean["lines"], "report": (record.get("report_paths") or {}).get("report")}


class PendingPage:
    """Sends started by page(); collect() returns the finished entries (waiting at most timeout s)."""

    def __init__(self, jobs, meta, key, log):
        self.jobs, self.meta, self.key, self.log = jobs, meta, key, log
        self.entries = None

    def done(self):
        return all(not t.is_alive() for _, _, t, _ in self.jobs)

    def collect(self, wait=0.0):
        """The entries once every send has ended, waiting up to wait seconds; None while still
        sending (wait=0). After a wait that ran out, an unfinished send is recorded as failed."""
        if self.entries is not None:
            return self.entries
        deadline = time.monotonic() + wait
        for _, _, t, _ in self.jobs:
            t.join(max(0.0, deadline - time.monotonic()))
        if not self.done() and not wait:
            return None
        self.entries = []
        for event, chan, t, box in self.jobs:
            entry = _finish(event, chan, t, box, self.meta, self.key)
            self.entries.append(entry)
            self.log(_line(event, entry))
        return self.entries


def page(record, step, log=print):
    """Start the page for step in the background and return a PendingPage, or None when it was
    already sent for this incident (also by an earlier run) or EMAIL_ON_APPROVAL is off. Returns
    at once: the approval prompt is never delayed by the mail server."""
    if not config.EMAIL_ON_APPROVAL:
        return None
    key = page_key(step)
    done = already_done(record)
    chans = [c for c in notifiers() if (PAGE, c.channel, key) not in done]
    meta = record.get("metadata") or {}
    if not chans:
        log(f"[notify] {PAGE} {key}: already paged for {meta.get('incident_id')}, not repeated")
        return None
    message = build_page(record, step)
    jobs = []
    for chan in chans:
        box = {}
        t = threading.Thread(target=_run, args=(chan, message, box), name=f"notify-page-{chan.channel}")
        t.start()
        jobs.append((PAGE, chan, t, box))
    return PendingPage(jobs, meta, key, log)


# --- python -m oncall emailtest ---------------------------------------------------------------------

def settings_lines():
    """The email settings the app loaded; secrets only as present/absent."""
    common = [f"EMAIL_ENABLED={'on' if config.EMAIL_ENABLED else 'off'}", f"EMAIL_TRANSPORT={config.EMAIL_TRANSPORT}"]
    if config.EMAIL_TRANSPORT == "resend":
        transport = [f"RESEND_API_KEY={'present' if config.RESEND_API_KEY else 'absent'}",
                     f"EMAIL_FROM={config.EMAIL_FROM or f'(not set: {config.RESEND_DEFAULT_FROM})'}"]
    else:
        transport = [f"SMTP_HOST={config.SMTP_HOST or '(not set)'}", f"SMTP_PORT={config.SMTP_PORT}",
                     f"SMTP_USER={config.SMTP_USER or '(not set: no login)'}",
                     f"SMTP_PASSWORD={'present' if config.SMTP_PASSWORD else 'absent'}",
                     f"SMTP_STARTTLS={'on' if config.SMTP_STARTTLS else 'off'}",
                     f"EMAIL_FROM={config.EMAIL_FROM or '(not set)'}"]
    return common + transport + [f"EMAIL_TO={', '.join(config.EMAIL_TO) or '(not set)'}",
                                 f"ONCALL_PRIMARY_NAME={config.ONCALL_PRIMARY_NAME or '(not set)'}",
                                 f"EMAIL_TIMEOUT_S={config.EMAIL_TIMEOUT_S:g}"]


def send_test():
    """One clearly labelled test email with the app's own settings. Never runs the agent and holds
    no incident data. Returns the notification entry (also logged to notifications.jsonl)."""
    if not config.EMAIL_ENABLED:
        entry = {"channel": "email", "recipients": list(config.EMAIL_TO), "status": "failed",
                 "error": "email failed: EMAIL_ENABLED is off (set EMAIL_ENABLED=on in .env)"}
    else:
        stamp = now_iso()
        via = "Resend" if config.EMAIL_TRANSPORT == "resend" else f"{config.SMTP_HOST}:{config.SMTP_PORT}"
        message = {"subject": f"[on-call TEST] email test from the AI on-call agent ({stamp})",
                   "lines": ["This is a TEST email from `python -m oncall emailtest`.",
                             "It is not an incident. Nothing was investigated or changed.", "",
                             f"Sent at {stamp} via {via}."]}
        box = {}
        t = threading.Thread(target=_run, args=(email_notifier(), message, box), name="notify-emailtest")
        t.start()
        t.join(config.EMAIL_TIMEOUT_S + 2)
        entry = dict(box) if not t.is_alive() else {
            "channel": "email", "recipients": list(config.EMAIL_TO), "status": "failed",
            "error": f"email failed: no answer within {config.EMAIL_TIMEOUT_S:g}s"}
    entry = scrub({"at": now_iso(), "event": "email_test", **entry})
    append_line(notify_log(), {"ts": entry["at"], "incident_id": None, "run_id": None, **entry})
    return entry
