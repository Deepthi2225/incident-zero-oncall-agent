"""Severity routing: the right people hear about each severity, and test messages reach nobody new."""
import json
from types import SimpleNamespace

import pytest

from oncall import config, notify

PLAN = {"people": {"a": {"email": "a@x.test", "phone": "+911111111111", "telegram": "1111111111"},
                   "b": {"email": "b@x.test", "phone": "+912222222222"},
                   "c": {"email": "c@x.test"}},
        "routes": {"SEV1": ["a", "b", "c"], "SEV3": ["c"], "unknown": ["b"]}}


class FakeHttp:
    def __init__(self, fail_for=None):
        self.calls, self.fail_for = [], fail_for

    def post(self, url, data=None, auth=None, timeout=None, json=None):
        self.calls.append(data or json)
        bad = self.fail_for and (data or json).get("To") == self.fail_for
        return SimpleNamespace(status_code=400 if bad else 200, text="",
                               json=lambda: {"message": "no"} if bad else {"sid": "CA1", "ok": True})


@pytest.fixture
def plan(tmp_path, monkeypatch):
    path = tmp_path / "escalation.json"
    path.write_text(json.dumps(PLAN), encoding="utf-8")
    monkeypatch.setattr(config, "ESCALATION_FILE", path)
    monkeypatch.setattr(config, "EMAIL_TO", ["fallback@x.test"])
    monkeypatch.setattr(config, "ONCALL_PRIMARY_PHONE", "+919999999999")
    for name, value in (("TWILIO_ACCOUNT_SID", "AC"), ("TWILIO_AUTH_TOKEN", "t"), ("TWILIO_FROM_NUMBER", "+15550001111")):
        monkeypatch.setattr(config, name, value)
    return path


def event(severity="SEV1 (critical service)", subject="[on-call SEV1] INC: x"):
    return {"subject": subject, "severity": severity, "title": "x", "lines": []}


def test_each_severity_reaches_its_own_people(plan):
    assert notify.contacts_for(event(), "email") == ["a@x.test", "b@x.test", "c@x.test"]
    assert notify.contacts_for(event("SEV3 (medium service)"), "email") == ["c@x.test"]


def test_a_severity_without_a_route_uses_unknown(plan):
    assert notify.contacts_for(event("SEV?"), "email") == ["b@x.test"]
    assert notify.contacts_for(event("unknown (service criticality not known)"), "email") == ["b@x.test"]


@pytest.mark.parametrize("message", [event(subject="[TEST] [on-call SEV1] INC: x"), event("test"),
                                     {"subject": "[on-call TEST] email test", "lines": []}])
def test_test_messages_stay_with_the_env_contacts(plan, message):
    assert notify.contacts_for(message, "email") == []


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", '{"routes": 5, "people": 7}',
                                     '{"routes": {"SEV1": ["a"]}, "people": {"a": "oops"}}'])
def test_a_broken_file_falls_back_to_env_and_never_raises(plan, content):
    plan.write_text(content, encoding="utf-8")
    assert notify.contacts_for(event(), "email") == []
    assert notify.EmailNotifier().for_event(event()).to == ["fallback@x.test"]


def test_no_file_changes_nothing(plan):
    plan.unlink()
    assert notify.EmailNotifier().for_event(event()).to == ["fallback@x.test"]


def test_routing_leaves_the_shared_channel_untouched(plan):
    chan = notify.EmailNotifier()
    assert chan.for_event(event()).to != chan.to and chan.to == ["fallback@x.test"]


def test_everyone_with_a_phone_on_the_route_is_called_and_masked(plan):
    http = FakeHttp()
    result = notify.CallNotifier(http=http).for_event(event()).notify(event())
    assert sorted(c["To"] for c in http.calls) == ["+911111111111", "+912222222222"]
    assert result["status"] == "sent" and "+911111111111" not in str(result) and len(result["recipients"]) == 2


def test_one_failed_call_marks_the_attempt_failed(plan):
    result = notify.CallNotifier(http=FakeHttp(fail_for="+912222222222")).for_event(event()).notify(event())
    assert result["status"] == "failed" and "+912222222222" not in result["error"]


def test_a_route_with_no_phones_still_calls_the_env_number(plan):
    http = FakeHttp()
    notify.CallNotifier(http=http).for_event(event("SEV3 (medium service)")).notify(event("SEV3 (medium service)"))
    assert [c["To"] for c in http.calls] == ["+919999999999"]


def test_telegram_follows_the_route(plan, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "tok")
    http = FakeHttp()
    result = notify.TelegramNotifier(http=http).for_event(event()).notify(event())
    assert [c["chat_id"] for c in http.calls] == ["1111111111"] and result["status"] == "sent"
