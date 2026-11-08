"""Telegram alerts through a fake Telegram: what is sent, when, and what is recorded."""
from types import SimpleNamespace

import pytest

from oncall import config, notify

SECRET = "123456:telegram-token-not-real"
CHAT = "987654321"


class FakeHttp:
    def __init__(self, status=200, body=None):
        self.status, self.body, self.calls = status, {"ok": True} if body is None else body, []

    def post(self, url, json=None, timeout=None):
        self.calls.append(SimpleNamespace(url=url, json=json, timeout=timeout))
        return SimpleNamespace(status_code=self.status, json=lambda: self.body)

    def get(self, url, timeout=None):
        self.calls.append(SimpleNamespace(url=url, timeout=timeout))
        return SimpleNamespace(status_code=self.status, json=lambda: self.body)


@pytest.fixture
def telegram(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", SECRET)
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", CHAT)


def event(subject="[on-call SEV1] INC-9: fix", lines=("Affected services: payment",)):
    return {"subject": subject, "severity": "SEV1 (critical service)", "title": "fix", "lines": list(lines)}


def test_sends_the_alert_and_never_records_the_full_chat_id(telegram):
    http = FakeHttp()
    result = notify.TelegramNotifier(http=http).notify(event())
    call = http.calls[0]
    assert call.url == f"https://api.telegram.org/bot{SECRET}/sendMessage" and call.json["chat_id"] == CHAT
    assert "INC-9" in call.json["text"] and "payment" in call.json["text"]
    assert result["status"] == "sent" and CHAT not in str(result) and SECRET not in str(result)


def test_only_live_incidents_are_sent(telegram):
    chan = notify.TelegramNotifier(http=FakeHttp())
    assert chan.wants(event()) and not chan.wants(event(subject="[TEST] [on-call SEV1] INC-9: fix"))


def test_long_alerts_fit_telegrams_limit():
    text = notify.telegram_text(event(lines=["x" * 9000]))
    assert len(text) <= 4096 and text.endswith(notify.TELEGRAM_CUT)


def test_email_only_wording_is_removed():
    text = notify.telegram_text(event(lines=["Report: attached as r.txt (also on disk)",
                                             "Approval is NOT possible by replying to this email."]))
    assert "attached" not in text and "replying to this message" in text


def test_missing_settings_fail_without_a_request(telegram, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
    http = FakeHttp()
    result = notify.TelegramNotifier(http=http).notify(event())
    assert result["status"] == "failed" and "TELEGRAM_CHAT_ID" in result["error"] and http.calls == []


def test_refusal_is_reported_without_token_or_chat(telegram):
    http = FakeHttp(status=400, body={"ok": False, "description": f"bad {SECRET} for {CHAT}"})
    result = notify.TelegramNotifier(http=http).notify(event())
    assert result["status"] == "failed" and "HTTP 400" in result["error"]
    assert SECRET not in result["error"] and CHAT not in result["error"]


def test_network_error_never_raises_or_leaks_the_token(telegram):
    class Broken:
        def post(self, *a, **kw):
            raise ConnectionError(f"failed for /bot{SECRET}/sendMessage")
    result = notify.TelegramNotifier(http=Broken()).notify(event())
    assert result["status"] == "failed" and SECRET not in result["error"]


def test_off_by_default_and_on_when_chosen(telegram, monkeypatch):
    for name in ("EMAIL_ENABLED", "CALLS_ENABLED", "TELEGRAM_ENABLED"):
        monkeypatch.setattr(config, name, False)
    assert [c.channel for c in notify.notifiers()] == ["log"]
    monkeypatch.setattr(config, "TELEGRAM_ENABLED", True)
    assert [c.channel for c in notify.notifiers()] == ["log", "telegram"]


def test_a_fixture_run_is_not_sent(telegram):
    http = FakeHttp()
    record = {"metadata": {"run_id": "R", "incident_id": "I", "mode": "test"}, "diagnosis": {"confidence": 0.1},
              "investigation": {"outcome": "llm_error"}, "escalations": [], "outcome": {}, "report_paths": {}}
    notify.dispatch(record, chans=[notify.TelegramNotifier(http=http)], log=lambda line: None)
    assert http.calls == []


def test_chat_lookup_lists_each_person_once(telegram):
    chat = {"id": 42, "first_name": "Sam"}
    http = FakeHttp(body={"ok": True, "result": [{"message": {"chat": chat}}, {"message": {"chat": chat}}]})
    assert notify.telegram_chats(http=http) == [("42", "Sam")]


def test_test_message_says_it_is_a_test(telegram, monkeypatch):
    http = FakeHttp()
    real = notify.TelegramNotifier
    monkeypatch.setattr(notify, "TelegramNotifier", lambda: real(http=http))
    assert notify.send_test_telegram()["status"] == "sent"
    assert "only a test" in http.calls[0].json["text"]
