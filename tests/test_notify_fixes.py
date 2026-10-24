"""Fixes for email escalation in live use, against the local fake SMTP server from test_notify:
no email lost at process exit, STARTTLS off really skipped, no login without a user, SMTP_HOST used
exactly as given ("localhost" too), .env parsing, every escalation path (including --test with
--non-interactive, watch mode, a human's N, a failed verify and the kill switch), one terminal
line per attempt, per-channel deduplication with retry after a failure, and `oncall emailtest`."""
import json
import os
import smtplib
import subprocess
import sys

import pytest

from oncall import config, notify
from oncall.action_api import FakeClient, KillSwitchRefused
from oncall.agent import run as agent_run
from oncall.record import load, save_existing
from oncall.remediate import make_approver
from oncall.verify import NOT_RECOVERED, RECOVERED
from tests.test_agent_loop import block_tool, make_agent, response
from tests.test_incident_record import ALERT
from tests.childenv import isolated_env
from tests.test_notify import FakeSMTP, edited, low, run, smtp  # noqa: F401  (smtp is a fixture)

TO = ["oncall@example.test", "lead@example.test"]


def emails(rec):
    return [n for n in rec["notifications"] if n["channel"] == "email"]


def child_env(server, tmp_path, **extra):
    """The environment of a separate `python` process: no repo .env (ONCALL_DOTENV=off), no setting
    or key from the developer's shell, SMTP to the fake server, and every other email or alias
    setting blank, so the child can talk only to the fake server."""
    return isolated_env(**{
        "EMAIL_ENABLED": "on", "EMAIL_TRANSPORT": "smtp", "SMTP_HOST": "127.0.0.1", "SMTP_PORT": server.port,
        "SMTP_USER": "", "SMTP_PASSWORD": "", "SMTP_PASS": "", "SMTP_STARTTLS": "off",
        "EMAIL_FROM": "agent@example.test", "EMAIL_TO": ",".join(TO), "EMAIL_TIMEOUT_S": "5",
        "RESEND_API_KEY": "", "NOTIFICATION_FROM_EMAIL": "", "ONCALL_PRIMARY_EMAIL": "", "ONCALL_PRIMARY_NAME": "",
        "EMAIL_ON_RECOVERED": "off", "ONCALL_RUNS_DIR": tmp_path / "runs", **extra})


# --- a. nothing is lost when the process exits right after the run --------------------------------

def test_dispatch_then_exit_still_delivers_to_a_slow_server(tmp_path):
    server = FakeSMTP("slow")  # takes 1 s to accept the message
    script = ("import sys\nfrom oncall import notify\n"
              "rec = {'metadata': {'run_id': 'r1', 'incident_id': 'INC-EXIT'},"
              " 'investigation': {'outcome': 'incomplete'}, 'notifications': []}\n"
              "notify.dispatch(rec)\nsys.exit(0)\n")
    out = subprocess.run([sys.executable, "-c", script], cwd=config.ROOT, env=child_env(server, tmp_path),
                         capture_output=True, text=True, timeout=60)
    server.shutdown()
    assert out.returncode == 0, out.stderr
    assert len(server.messages) == 1 and "INC-EXIT" in server.messages[0]["msg"]["Subject"]
    assert "[notify] low_confidence: email sent to oncall@example.test, lead@example.test" in out.stdout


def test_emailtest_command_sends_one_labelled_email_and_exits(tmp_path):
    server = FakeSMTP("slow")
    out = subprocess.run([sys.executable, "-m", "oncall", "emailtest"], cwd=config.ROOT,
                         env=child_env(server, tmp_path), capture_output=True, text=True, timeout=60)
    server.shutdown()
    assert out.returncode == 0, out.stderr
    assert "SMTP_PASSWORD=absent" in out.stdout and "sent to oncall@example.test, lead@example.test" in out.stdout
    msg = server.messages[0]["msg"]
    assert msg["Subject"].startswith("[on-call TEST]") and "not an incident" in msg.get_payload(decode=True).decode()
    assert not (tmp_path / "runs" / "index.jsonl").exists()           # the agent did not run
    line = json.loads((tmp_path / "runs" / "notifications.jsonl").read_text(encoding="utf-8"))
    assert (line["event"], line["status"]) == ("email_test", "sent")


@pytest.mark.parametrize("enabled, expect", [("off", "EMAIL_ENABLED is off"), ("enabled", "is not on/off")])
def test_emailtest_says_exactly_why_nothing_was_sent(tmp_path, enabled, expect):
    server = FakeSMTP()
    out = subprocess.run([sys.executable, "-m", "oncall", "emailtest"], cwd=config.ROOT,
                         env=child_env(server, tmp_path, EMAIL_ENABLED=enabled), capture_output=True,
                         text=True, timeout=60)
    server.shutdown()
    assert out.returncode == 1 and server.messages == []
    assert expect in out.stdout + out.stderr


def test_emailtest_reports_the_exact_connection_error(tmp_path):
    server = FakeSMTP()
    server.shutdown()
    server.server_close()  # nothing listens on that port any more
    out = subprocess.run([sys.executable, "-m", "oncall", "emailtest"], cwd=config.ROOT,
                         env=child_env(server, tmp_path), capture_output=True, text=True, timeout=60)
    assert out.returncode == 1
    assert "email failed: ConnectionRefusedError" in out.stdout and "nothing is listening on 127.0.0.1" in out.stdout


# --- b. SMTP_HOST exactly as given ------------------------------------------------------------------

@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_smtp_host_is_used_exactly_as_given(payment_down, monkeypatch, smtp, host):
    server = smtp(timeout=8)  # localhost may try ::1 first: about 2 s on Windows before 127.0.0.1
    monkeypatch.setattr(config, "SMTP_HOST", host)
    seen, real = [], smtplib.SMTP.connect
    monkeypatch.setattr(smtplib.SMTP, "connect", lambda self, h="localhost", p=0, *a: seen.append(h) or real(self, h, p, *a))
    rec = run(monkeypatch, script=edited(low))
    assert seen == [host]
    assert emails(rec)[0]["status"] == "sent" and len(server.messages) == 1


# --- c. settings ---------------------------------------------------------------------------------

def test_starttls_off_is_skipped_and_no_login_without_a_user(payment_down, monkeypatch, smtp):
    server = smtp()
    rec = run(monkeypatch, script=edited(low))
    assert emails(rec)[0]["status"] == "sent"
    assert "STARTTLS" not in server.commands and "AUTH" not in server.commands
    assert server.commands[:2] == ["EHLO", "MAIL"]


def test_starttls_on_against_a_plain_server_fails_with_a_hint(payment_down, monkeypatch, smtp, capsys):
    server = smtp()
    monkeypatch.setattr(config, "SMTP_STARTTLS", True)
    rec = run(monkeypatch, script=edited(low))
    err = emails(rec)[0]["error"]
    assert err.startswith("email failed: SMTPNotSupportedError") and "SMTP_STARTTLS=off" in err
    assert server.messages == [] and f"[notify] low_confidence: {err}" in capsys.readouterr().out


def test_login_only_when_a_user_is_set(payment_down, monkeypatch, smtp):
    smtp()
    monkeypatch.setattr(config, "SMTP_USER", "agent")
    calls = []
    monkeypatch.setattr(smtplib.SMTP, "login", lambda self, u, p, **k: calls.append(u))
    rec = run(monkeypatch, script=edited(low))
    assert calls == ["agent"] and emails(rec)[0]["status"] == "sent"


def test_dotenv_values_and_on_off_parsing(tmp_path, monkeypatch, capsys):
    env = tmp_path / ".env"
    env.write_text("EMAIL_ENABLED=on   # comment\nSMTP_HOST='127.0.0.1'\nEMAIL_TO=\"a@x.test # not a comment\"\n"
                   "EMAIL_TIMEOUT_S=10    # per send\nSMTP_STARTTLS=OFF\n# SMTP_USER=commented\nEMPTY=\n",
                   encoding="utf-8")
    for k in ("EMAIL_ENABLED", "SMTP_HOST", "EMAIL_TO", "EMAIL_TIMEOUT_S", "SMTP_STARTTLS", "SMTP_USER", "EMPTY"):
        monkeypatch.delenv(k, raising=False)
    config.load_dotenv(env)
    assert [os.environ.get(k) for k in ("EMAIL_ENABLED", "SMTP_HOST", "EMAIL_TO", "EMAIL_TIMEOUT_S", "SMTP_STARTTLS")] \
        == ["on", "127.0.0.1", "a@x.test # not a comment", "10", "OFF"]
    assert "SMTP_USER" not in os.environ and "EMPTY" not in os.environ
    for raw, want in [("on", True), ("ON", True), ("true", True), ("1", True), ("yes", True),
                      ("off", False), ("False", False), ("0", False), ("no", False)]:
        monkeypatch.setenv("X_FLAG", raw)
        assert config.env_flag("X_FLAG", not want) is want
    monkeypatch.setenv("X_FLAG", "enabled")
    assert config.env_flag("X_FLAG", False) is False and "X_FLAG='enabled' is not on/off" in capsys.readouterr().err


# --- d. every escalation path calls the notifier ----------------------------------------------------

def test_non_interactive_run_needs_approval_and_emails(payment_down, monkeypatch, smtp):
    server = smtp()
    rec = run(monkeypatch, approve=make_approver(interactive=False))
    assert rec["remediation_plan"]["steps"][0]["approval"]["status"] == "pending"
    assert [(n["event"], n["status"]) for n in emails(rec)] == [("approval_needed", "sent")]
    assert "waiting for a human approval" in server.messages[0]["msg"]["Subject"]


def test_cli_test_mode_non_interactive_emails(payment_down, monkeypatch, smtp, capsys):
    """python -m oncall.agent.run alert.json --test --non-interactive, with a fake LLM."""
    server = smtp()
    make_agent(monkeypatch, edited(low))
    alert = config.ROOT / "tests" / "fixtures" / "payment_down" / "alert.json"
    monkeypatch.setattr(sys, "argv", ["run", str(alert), "--test", "--fixtures",
                                      str(config.ROOT / "tests" / "fixtures" / "payment_down"), "--non-interactive"])
    with pytest.raises(SystemExit):
        agent_run.main()
    assert len(server.messages) == 1 and "email sent to" in capsys.readouterr().out


def test_watch_mode_entry_point_emails(payment_down, monkeypatch, smtp):
    """Watch mode calls run_agent(alert) with the default (terminal) approver; without a terminal
    nobody can approve, which is an escalation."""
    server = smtp()
    make_agent(monkeypatch, edited(lambda inp: None))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    rec = agent_run.run_agent(dict(ALERT))
    assert [n["event"] for n in emails(rec)] == ["approval_needed"] and len(server.messages) == 1


def test_human_answers_n_at_the_terminal(payment_down, monkeypatch, smtp):
    server = smtp()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    rec = run(monkeypatch, approve=make_approver())
    assert [n["event"] for n in emails(rec)] == ["human_rejected"]
    assert "rejected by the on-call engineer" in server.messages[0]["msg"].get_payload(decode=True).decode()


def test_failed_verify_after_rollback(payment_down, monkeypatch, smtp):
    server = smtp()
    rec = run(monkeypatch, client=FakeClient(live=True), outcome=NOT_RECOVERED)
    assert [n["event"] for n in emails(rec)] == ["verification_failed"]
    assert "verified as not_recovered; rollback: " in server.messages[0]["msg"].get_payload(decode=True).decode()


def test_failed_manual_verify_emails(payment_down, monkeypatch, smtp):
    server = smtp()
    rec = run(monkeypatch, client=FakeClient(live=True))   # recovered: no email
    assert server.messages == []
    record = load(rec["path"])
    record["manual_verifications"].append({"at": "2026-10-24T10:00:00+00:00", "outcome": NOT_RECOVERED, "targets": []})
    saved = save_existing(record, rec["path"])
    assert [n["event"] for n in saved["notifications"] if n["channel"] == "email"] == ["verification_failed"]
    assert json.loads(open(rec["path"], encoding="utf-8").read())["notifications"] == saved["notifications"]


def test_kill_switch_turned_on_while_a_step_waits(payment_down, monkeypatch, smtp):
    server = smtp()

    def approve_then_stop(step, diag):  # the human approves, then someone hits the kill switch
        config.KILL_SWITCH_FILE.write_text("stop\n")
        return {"status": "approved", "approved_by": "human"}
    rec = run(monkeypatch, client=FakeClient(live=True), approve=approve_then_stop)
    assert rec["remediation_plan"]["steps"][0]["execution"]["reason"] == "kill switch is on"
    assert [n["event"] for n in emails(rec)] == ["kill_switch"] and len(server.messages) == 1


def test_action_api_kill_switch_refusal_emails(payment_down, monkeypatch, smtp):
    smtp()

    class Locked(FakeClient):
        def call(self, *a, **k):
            raise KillSwitchRefused("refused: kill switch is on (Action API)")
    rec = run(monkeypatch, client=Locked(live=True))
    assert [n["event"] for n in emails(rec)] == ["kill_switch"]


# --- e. one line per attempt, and retries ------------------------------------------------------------

def test_email_off_says_so_in_the_terminal(payment_down, monkeypatch, capsys):
    run(monkeypatch, script=edited(low))
    out = capsys.readouterr().out
    assert "[notify] low_confidence: logged to" in out and "(email off: EMAIL_ENABLED is not on)" in out


def test_logged_while_email_was_off_still_emails_later(payment_down, monkeypatch, smtp, capsys):
    server = smtp()
    monkeypatch.setattr(config, "EMAIL_ENABLED", False)
    run(monkeypatch, script=edited(low))                        # same incident, email off: logged
    monkeypatch.setattr(config, "EMAIL_ENABLED", True)
    rec = run(monkeypatch, script=edited(low))                  # email on now: the email goes out
    assert [(n["channel"], n["status"]) for n in rec["notifications"]] == [("email", "sent")]
    assert len(server.messages) == 1
    run(monkeypatch, script=edited(low))                        # and only once
    assert len(server.messages) == 1 and "already notified for INC-7" in capsys.readouterr().out


def test_failed_email_is_retried_by_the_next_run(payment_down, monkeypatch, smtp):
    smtp("reject")
    first = run(monkeypatch, script=edited(low))
    assert emails(first)[0]["status"] == "failed"
    server = smtp()                                             # the mail server is fixed
    second = run(monkeypatch, script=edited(low))
    assert emails(second)[0]["status"] == "sent" and len(server.messages) == 1


def test_recovered_run_has_no_escalation_email(payment_down, monkeypatch, smtp):
    server = smtp()
    rec = run(monkeypatch, client=FakeClient(live=True), outcome=RECOVERED)
    assert rec["outcome"]["status"] == "recovered" and server.messages == []
