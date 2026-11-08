"""Resend as the second email transport, with a fake HTTP client (nothing leaves the laptop):
request shape, the API key only in the Authorization header and never printed or logged, failures
recorded without crashing, our teammate's .env names as aliases, and whitespace stripped."""
import json
import tempfile
from pathlib import Path

import pytest
import requests

from oncall import __main__ as cli
from oncall import config, notify
from oncall.record import scrub
from tests.test_notify import edited, low, run

KEY = "re_TESTKEY_1234567890abcdefXYZ"


class FakeResponse:
    def __init__(self, status, data=None, text=""):
        self.status_code, self._data, self.text = status, data, text

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


class FakeHTTP:
    """Stands in for requests.post; replies with the scripted response or raises."""

    def __init__(self, response=None, error=None):
        self.response = response or FakeResponse(200, {"id": "4ef9a417-02e9-4d39-ad75-9611e0fcc33c"})
        self.error, self.calls = error, []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        if self.error:
            raise self.error
        return self.response


@pytest.fixture
def resend(monkeypatch):
    def start(**kw):
        fake = FakeHTTP(**kw)
        monkeypatch.setattr(requests, "post", fake.post)  # the only way out, replaced
        monkeypatch.setattr(config, "EMAIL_ENABLED", True)
        monkeypatch.setattr(config, "EMAIL_TRANSPORT", "resend")
        monkeypatch.setattr(config, "RESEND_API_KEY", KEY)
        monkeypatch.setattr(config, "EMAIL_FROM", "")
        monkeypatch.setattr(config, "EMAIL_TO", ["owner@example.test"])
        return fake
    return start


def everything_written(rec, capsys):
    """Terminal output, the record, its report and notifications.jsonl, as one string."""
    out = capsys.readouterr()
    report = Path(rec["report_paths"]["report"]).read_text(encoding="utf-8")
    return out.out + out.err + json.dumps(rec) + report + notify.notify_log().read_text(encoding="utf-8")


def test_request_shape_and_sent_line(payment_down, monkeypatch, resend, capsys):
    fake = resend()
    rec = run(monkeypatch, script=edited(low))
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["url"] == "https://api.resend.com/emails" and call["timeout"] == 10.0
    assert call["headers"] == {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
    body = call["json"]
    assert set(body) == {"from", "to", "subject", "text", "html", "attachments"}
    assert body["from"] == "AI On-Call Agent <onboarding@resend.dev>" and body["to"] == ["owner@example.test"]
    assert "INC-7" in body["subject"] and "low confidence" in body["subject"]
    assert "Incident: INC-7" in body["text"] and "Top evidence:" in body["text"]
    mail = [n for n in rec["notifications"] if n["channel"] == "email"][0]
    assert (mail["status"], mail["transport"], mail["message_id"]) == \
        ("sent", "resend", "4ef9a417-02e9-4d39-ad75-9611e0fcc33c")
    assert ("[notify] low_confidence: email sent (resend id 4ef9a417-02e9-4d39-ad75-9611e0fcc33c) "
            "to owner@example.test") in capsys.readouterr().out


def test_api_key_is_never_printed_or_logged(payment_down, monkeypatch, resend, capsys):
    # even when Resend's error message echoes the key back
    resend(response=FakeResponse(401, {"statusCode": 401, "name": "validation_error",
                                       "message": f"API key is invalid: {KEY}"}))
    rec = run(monkeypatch, script=edited(low))
    text = everything_written(rec, capsys)
    assert KEY not in text and "Bearer re_" not in text
    assert "email failed: Resend HTTP 401: API key is invalid: <redacted>" in text


def test_scrubber_knows_resend_keys(monkeypatch):
    monkeypatch.setattr(config, "RESEND_API_KEY", KEY)
    assert scrub({"note": f"key {KEY}", "other": "re_abcdefghijklmnop1234"}) == \
        {"note": "key <redacted>", "other": "<redacted>"}


@pytest.mark.parametrize("kw, expect", [
    ({"response": FakeResponse(403, {"statusCode": 403, "name": "validation_error",
                                     "message": "You can only send testing emails to your own email address"})},
     "Resend HTTP 403: You can only send testing emails to your own email address (Resend's onboarding@resend.dev"),
    ({"response": FakeResponse(500, None, "upstream exploded")}, "Resend HTTP 500: upstream exploded"),
    ({"response": FakeResponse(200, {"unexpected": True})}, "Resend HTTP 200: no details"),
    ({"error": requests.ConnectionError("connection reset")}, "Resend ConnectionError: connection reset"),
    ({"error": requests.Timeout("read timed out")}, "Resend Timeout: read timed out"),
])
def test_failures_are_recorded_and_the_run_completes(payment_down, monkeypatch, resend, capsys, kw, expect):
    resend(**kw)
    rec = run(monkeypatch, script=edited(low))
    assert rec["status"] == "complete" and rec["outcome"]["status"] == "escalated"
    mail = [n for n in rec["notifications"] if n["channel"] == "email"][0]
    assert mail["status"] == "failed" and mail["error"].startswith("email failed: ")
    assert expect in mail["error"] and f"[notify] low_confidence: {mail['error']}" in capsys.readouterr().out
    line = json.loads(notify.notify_log().read_text(encoding="utf-8").splitlines()[-1])
    assert (line["channel"], line["status"], line["transport"]) == ("email", "failed", "resend")


def test_missing_key_or_unknown_transport_fails_without_a_request(payment_down, monkeypatch, resend):
    fake = resend()
    monkeypatch.setattr(config, "RESEND_API_KEY", "")
    rec = run(monkeypatch, script=edited(low))
    assert [n["error"] for n in rec["notifications"] if n["channel"] == "email"] == \
        ["email failed: RESEND_API_KEY not set"]
    monkeypatch.setattr(config, "EMAIL_TRANSPORT", "carrier-pigeon")
    rec = run(monkeypatch, script=edited(low), alert={"incident_id": "INC-PIGEON", "service": "checkout"})
    assert "EMAIL_TRANSPORT='carrier-pigeon' is not smtp or resend" in \
        [n for n in rec["notifications"] if n["channel"] == "email"][0]["error"]
    assert fake.calls == []


def test_emailtest_works_with_resend(payment_down, monkeypatch, resend, capsys):
    fake = resend()
    cli.cmd_emailtest(None)
    out = capsys.readouterr().out
    assert "EMAIL_TRANSPORT=resend" in out and "RESEND_API_KEY=present" in out and KEY not in out
    assert "sent (resend id 4ef9a417-02e9-4d39-ad75-9611e0fcc33c) to owner@example.test" in out
    assert fake.calls[0]["json"]["subject"].startswith("[on-call TEST]")
    assert "via Resend" in fake.calls[0]["json"]["text"]


def test_emailtest_reports_a_resend_failure(payment_down, monkeypatch, resend, capsys):
    resend(response=FakeResponse(422, {"name": "missing_required_field", "message": "Missing `to` field."}))
    with pytest.raises(SystemExit):
        cli.cmd_emailtest(None)
    assert "email failed: Resend HTTP 422: Missing `to` field." in capsys.readouterr().out


# --- our teammate's .env names, and whitespace --------------------------------------------------------

EMAIL_NAMES = ["EMAIL_ENABLED", "EMAIL_TRANSPORT", "EMAIL_FROM", "EMAIL_TO", "RESEND_API_KEY", "SMTP_HOST",
               "SMTP_USER", "SMTP_PASSWORD", "SMTP_PASS", "NOTIFICATION_FROM_EMAIL", "ONCALL_PRIMARY_EMAIL",
               "ONCALL_PRIMARY_NAME", "ONCALL_PRIMARY_PHONE", "NOTIFY_ALL_CHANNELS", "EMAIL_TIMEOUT_S", "SMTP_PORT"]


def load_config(monkeypatch, **env):
    """oncall/config.py evaluated with exactly these email variables (and no .env file)."""
    for name in EMAIL_NAMES:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    src = Path(config.__file__).read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as tmp:  # ROOT/.env does not exist there
        ns = {"__file__": str(Path(tmp, "oncall", "config.py")), "__name__": "_config_under_test"}
        exec(compile(src, config.__file__, "exec"), ns)
    return ns


def test_teammate_names_are_aliases(monkeypatch):
    c = load_config(monkeypatch, RESEND_API_KEY="re_alias_key_000000000000", NOTIFICATION_FROM_EMAIL="Agent <a@example.test>",
                    ONCALL_PRIMARY_EMAIL="primary@example.test", SMTP_PASS="pw-from-alias",
                    ONCALL_PRIMARY_NAME="Alex", ONCALL_PRIMARY_PHONE="+15550000", NOTIFY_ALL_CHANNELS="true")
    assert c["EMAIL_FROM"] == "Agent <a@example.test>" and c["EMAIL_TO"] == ["primary@example.test"]
    assert c["SMTP_PASSWORD"] == "pw-from-alias" and c["RESEND_API_KEY"] == "re_alias_key_000000000000"
    assert c["ONCALL_PRIMARY_NAME"] == "Alex" and c["EMAIL_TRANSPORT"] == "smtp"
    assert c["ONCALL_PRIMARY_PHONE"] == "+15550000" and c["CALLS_ENABLED"] is False  # the phone rings only when chosen
    assert "NOTIFY_ALL_CHANNELS" not in c


def test_our_names_win_and_primary_email_is_added_once(monkeypatch):
    c = load_config(monkeypatch, EMAIL_FROM="ours@example.test", NOTIFICATION_FROM_EMAIL="theirs@example.test",
                    EMAIL_TO="a@example.test, b@example.test", ONCALL_PRIMARY_EMAIL="b@example.test,c@example.test",
                    SMTP_PASSWORD="ours", SMTP_PASS="theirs")
    assert c["EMAIL_FROM"] == "ours@example.test" and c["SMTP_PASSWORD"] == "ours"
    assert c["EMAIL_TO"] == ["a@example.test", "b@example.test", "c@example.test"]


def test_whitespace_is_stripped_everywhere(monkeypatch):
    c = load_config(monkeypatch, EMAIL_TRANSPORT="  Resend \t", RESEND_API_KEY="  re_spaced_key_0000000000  ",
                    NOTIFICATION_FROM_EMAIL="  from@example.test ", EMAIL_TO=" a@example.test ,  ,b@example.test ",
                    ONCALL_PRIMARY_EMAIL="  c@example.test  ", ONCALL_PRIMARY_NAME="  Alex  ",
                    SMTP_HOST=" 127.0.0.1 ", SMTP_USER=" u ", SMTP_PASS="  pw  ", SMTP_PORT=" 1025 ",
                    EMAIL_TIMEOUT_S=" 7 ", EMAIL_FROM="   ")
    assert c["EMAIL_TRANSPORT"] == "resend" and c["RESEND_API_KEY"] == "re_spaced_key_0000000000"
    assert c["EMAIL_FROM"] == "from@example.test"  # a blank EMAIL_FROM falls back to the alias
    assert c["EMAIL_TO"] == ["a@example.test", "b@example.test", "c@example.test"]
    assert (c["ONCALL_PRIMARY_NAME"], c["SMTP_HOST"], c["SMTP_USER"], c["SMTP_PASSWORD"]) == ("Alex", "127.0.0.1", "u", "pw")
    assert (c["SMTP_PORT"], c["EMAIL_TIMEOUT_S"]) == (1025, 7.0)


def test_greeting_uses_the_primary_name(payment_down, monkeypatch, resend):
    fake = resend()
    monkeypatch.setattr(config, "ONCALL_PRIMARY_NAME", "Alex")
    run(monkeypatch, script=edited(low))
    assert fake.calls[0]["json"]["text"].startswith("Hello Alex,\n\n*** TEST ONLY")
