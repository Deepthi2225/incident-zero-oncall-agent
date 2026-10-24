"""Email escalation (oncall/notify.py) against a local fake SMTP server: one email per escalation
type, scrubbed content, failures and timeouts never break a run, EMAIL_ENABLED=off sends nothing,
no duplicates (also across a restart), and the dashboard shows notifications safely."""
import email
import json
import socket
import socketserver
import threading
import time

import pytest

from oncall import config, notify
from oncall.action_api import FakeClient
from oncall.ui.server import route
from oncall.verify import NOT_RECOVERED, RECOVERED
from tests.schema_check import errors
from tests.test_agent_loop import block_tool, make_agent, response
from tests.test_incident_record import ALERT, diagnosis_script
from tests.test_remediate import StubVerifier, human

SECRET = "sk-ant-api03-NOTIFYSECRET123456"
SMTP_SECRET = "SMTPPASS-987654"


class FakeSMTP(socketserver.ThreadingTCPServer):
    """Just enough SMTP for smtplib: EHLO, MAIL, RCPT, DATA, QUIT. mode: ok | reject | hang."""
    daemon_threads = allow_reuse_address = True

    def __init__(self, mode="ok"):
        self.mode, self.messages = mode, []
        super().__init__(("127.0.0.1", 0), SMTPHandler)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def port(self):
        return self.server_address[1]


class SMTPHandler(socketserver.StreamRequestHandler):
    def reply(self, line):
        self.wfile.write((line + "\r\n").encode())

    def handle(self):
        if self.server.mode == "hang":  # accepts the connection, never greets
            time.sleep(5)
            return
        self.reply("220 fake ESMTP")
        rcpts = []
        while True:
            line = self.rfile.readline().decode(errors="replace").strip()
            cmd = line[:4].upper()
            if not line or cmd == "QUIT":
                self.reply("221 bye")
                return
            if cmd in ("EHLO", "HELO"):
                self.reply("250 fake")
            elif cmd == "MAIL":
                self.reply("554 rejected by policy" if self.server.mode == "reject" else "250 ok")
            elif cmd == "RCPT":
                rcpts.append(line.split(":", 1)[1].strip(" <>"))
                self.reply("250 ok")
            elif cmd == "DATA":
                self.reply("354 go ahead")
                data = []
                while (chunk := self.rfile.readline()) not in (b".\r\n", b""):
                    data.append(chunk)
                self.server.messages.append({"rcpts": rcpts, "msg": email.message_from_bytes(b"".join(data))})
                rcpts = []
                self.reply("250 queued")
            else:
                self.reply("250 ok")


@pytest.fixture
def smtp(monkeypatch):
    servers = []

    def start(mode="ok", timeout=3.0):
        server = FakeSMTP(mode)
        servers.append(server)
        monkeypatch.setattr(config, "EMAIL_ENABLED", True)
        monkeypatch.setattr(config, "SMTP_HOST", "127.0.0.1")
        monkeypatch.setattr(config, "SMTP_PORT", server.port)
        monkeypatch.setattr(config, "SMTP_USER", "")
        monkeypatch.setattr(config, "SMTP_PASSWORD", SMTP_SECRET)
        monkeypatch.setattr(config, "SMTP_STARTTLS", False)
        monkeypatch.setattr(config, "EMAIL_FROM", "agent@example.test")
        monkeypatch.setattr(config, "EMAIL_TO", ["oncall@example.test", "lead@example.test"])
        monkeypatch.setattr(config, "EMAIL_TIMEOUT_S", timeout)
        return server
    yield start
    for s in servers:
        s.shutdown()
        s.server_close()


# --- runs that end in each kind of escalation ---------------------------------------------------------

def edited(change):
    """diagnosis_script with the submitted diagnosis changed by change(input)."""
    def script(n, kwargs):
        out = diagnosis_script(n, kwargs)
        if n == 2:
            for b in out.content:
                if b.type == "tool_use":
                    change(b.input)
        return out
    return script


def low(inp):
    inp["root_causes"][0]["confidence"] = 0.3


def network(inp):
    inp["root_causes"][0]["fault_class"] = "network_degradation"


def leaky(inp):  # text the model might echo from telemetry: a key, the SMTP password, flag data
    inp["root_causes"][0]["confidence"] = 0.3
    inp["root_causes"][0]["cause"] = f"payment exited; env had {SECRET} and {SMTP_SECRET}"
    inp["open_questions"] = ["Was feature_flag.paymentFailure on?", "Why did payment exit?"]


def reviewer_script(n, kwargs):
    if n <= 2:
        out = diagnosis_script(n, kwargs)
        if n == 2:
            for b in out.content:
                if b.type == "tool_use":
                    b.input["root_causes"][0]["confidence"] = 0.8
        return out
    return response(block_tool("r", "submit_review", {
        "verdict": "disagree", "confidence_adjustment": -0.3,
        "objections": [{"text": "checkout errors started before payment stopped", "evidence": ["E1"]}]}))


def run(monkeypatch, script=diagnosis_script, client=None, approve=None, outcome=RECOVERED, alert=ALERT):
    agent, _ = make_agent(monkeypatch, script)
    agent.action_client = client or FakeClient(live=False)
    agent.approve = approve or human({})
    agent.verifier = StubVerifier({"payment": outcome})
    rec = agent.run(dict(alert))
    assert errors({k: v for k, v in rec.items() if k != "path"}) == []
    return rec


def kill_switch_on(monkeypatch):
    config.KILL_SWITCH_FILE.write_text("stop\n")


CASES = {
    "low_confidence": dict(script=edited(low)),
    "no_allowlisted_fix": dict(script=edited(network)),
    "verification_failed": dict(client=FakeClient(live=True), outcome=NOT_RECOVERED),
    "human_rejected": dict(approve=human({"payment": False})),
}


@pytest.mark.parametrize("event", ["low_confidence", "no_allowlisted_fix", "verification_failed",
                                   "human_rejected", "kill_switch", "reviewer_objection"])
def test_each_escalation_sends_exactly_one_email(payment_down, monkeypatch, smtp, event):
    server = smtp()
    if event == "kill_switch":
        kill_switch_on(monkeypatch)
        rec = run(monkeypatch)
    elif event == "reviewer_objection":
        monkeypatch.setattr(config, "REVIEWER", True)
        rec = run(monkeypatch, script=reviewer_script)
    else:
        kwargs = dict(CASES[event])
        if "client" in kwargs:
            kwargs["client"] = FakeClient(live=True)
        rec = run(monkeypatch, **kwargs)

    assert len(server.messages) == 1
    m = server.messages[0]
    assert m["rcpts"] == ["oncall@example.test", "lead@example.test"]
    assert notify.TITLES[event] in m["msg"]["Subject"] and "INC-7" in m["msg"]["Subject"]
    body = m["msg"].get_payload(decode=True).decode()
    for part in ("Incident: INC-7", "Severity: ", "Why: ", "Affected services: ", "Root cause: ",
                 "Top evidence:", "Open questions:", "Next steps:", "Report: "):
        assert part in body
    sent = [n for n in rec["notifications"] if n["channel"] == "email"]
    assert [(n["event"], n["status"], n["recipients"]) for n in sent] == \
        [(event, "sent", ["oncall@example.test", "lead@example.test"])]
    assert [n["event"] for n in rec["notifications"] if n["channel"] == "log"] == [event]


def test_content_is_scrubbed_and_specific(payment_down, monkeypatch, smtp):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    server = smtp()
    rec = run(monkeypatch, script=edited(leaky))
    msg = server.messages[0]["msg"]
    body = msg.get_payload(decode=True).decode()
    everything = str(msg) + body + json.dumps(rec) + notify.notify_log().read_text(encoding="utf-8")
    assert SECRET not in everything and SMTP_SECRET not in everything
    assert "feature_flag" not in everything.lower()
    assert "Severity: SEV1 (critical service)" in body           # payment is critical
    assert "Affected services: checkout" in body
    assert "confidence 0.3" in body and "below 0.6" in body
    assert "  - E" in body                                       # cited evidence lines
    assert rec["report_paths"]["report"] in body


@pytest.mark.parametrize("mode", ["reject", "hang", "closed"])
def test_email_failure_never_breaks_the_run(payment_down, monkeypatch, smtp, mode):
    if mode == "closed":
        smtp()
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        monkeypatch.setattr(config, "SMTP_PORT", sock.getsockname()[1])  # nothing listens here
        sock.close()
    else:
        smtp(mode, timeout=0.5)
    t0 = time.monotonic()
    rec = run(monkeypatch, script=edited(low))
    assert time.monotonic() - t0 < 10
    assert rec["status"] == "complete" and rec["outcome"]["status"] == "escalated"
    mail = [n for n in rec["notifications"] if n["channel"] == "email"][0]
    assert mail["status"] == "failed" and mail["error"].startswith("email failed: ")
    log = [json.loads(x) for x in notify.notify_log().read_text(encoding="utf-8").splitlines()]
    assert any(x["channel"] == "email" and x["status"] == "failed" for x in log)


def test_email_off_sends_nothing_but_logs(payment_down, monkeypatch, smtp):
    server = smtp()
    monkeypatch.setattr(config, "EMAIL_ENABLED", False)
    rec = run(monkeypatch, script=edited(low))
    assert server.messages == []
    assert [(n["channel"], n["status"]) for n in rec["notifications"]] == [("log", "logged")]
    line = json.loads(notify.notify_log().read_text(encoding="utf-8").splitlines()[0])
    assert (line["event"], line["incident_id"], line["channel"]) == ("low_confidence", "INC-7", "log")


def test_no_duplicates_in_a_run_or_after_a_restart(payment_down, monkeypatch, smtp):
    server = smtp()
    first = run(monkeypatch, script=edited(low))
    assert len(server.messages) == 1
    assert notify.dispatch(first, log=lambda *_: None) == []    # same record again: nothing new
    second = run(monkeypatch, script=edited(low))               # restart: same incident, new run
    assert len(server.messages) == 1 and second["notifications"] == []
    run(monkeypatch, script=edited(low), alert={**ALERT, "incident_id": "INC-8"})
    assert len(server.messages) == 2                            # a different incident still emails


def test_recovered_info_email_only_when_enabled(payment_down, monkeypatch, smtp):
    server = smtp()
    live = FakeClient(live=True)
    rec = run(monkeypatch, client=live)
    assert rec["outcome"]["status"] == "recovered" and server.messages == [] and rec["notifications"] == []
    monkeypatch.setattr(config, "EMAIL_ON_RECOVERED", True)
    run(monkeypatch, client=FakeClient(live=True), alert={**ALERT, "incident_id": "INC-9"})
    assert len(server.messages) == 1 and "recovered (info)" in server.messages[0]["msg"]["Subject"]


def test_dry_run_alone_is_not_an_escalation_email(payment_down, monkeypatch, smtp):
    server = smtp()
    rec = run(monkeypatch)  # confident, approved, dry run
    assert server.messages == [] and rec["notifications"] == []


def test_dashboard_shows_notifications_safely(payment_down, monkeypatch, smtp):
    smtp()
    rec = run(monkeypatch, script=edited(low))
    with open(notify.notify_log(), "a", encoding="utf-8") as f:  # a line nobody should trust
        f.write(json.dumps({"ts": "x", "event": "<img src=x onerror=alert(1)>", "subject": f"key {SECRET}",
                            "why": "feature_flag.paymentFailure"}) + "\n")
        f.write("not json\n")
    status, ctype, raw = route("/api/notifications")
    text = raw.decode()
    assert status == 200 and ctype.startswith("application/json")
    rows = json.loads(text)["notifications"]
    assert any(r.get("event") == "low_confidence" and r.get("channel") == "email" for r in rows)
    assert SECRET not in text and "feature_flag" not in text.lower()
    assert rows[-1] == {"unparsed": "not json"}
    status, _, raw = route("/api/incident/" + rec["metadata"]["run_id"])
    assert status == 200 and json.loads(raw)["notifications"]
    page = route("/")[2].decode()
    assert "refreshNotifications" in page and "innerHTML" not in page
