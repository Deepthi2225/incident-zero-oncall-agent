"""Voice calls through a fake Twilio: when they ring, what they say, what is recorded."""
from types import SimpleNamespace
from urllib.parse import unquote_plus

import pytest

from oncall import config, notify

SECRET = "twilio-token-not-real"
NUMBER = "+919000012345"


class FakeHttp:
    def __init__(self, status=201, body=None):
        self.status, self.body, self.calls = status, {"sid": "CA123"} if body is None else body, []

    def post(self, url, data=None, auth=None, timeout=None):
        self.calls.append(SimpleNamespace(url=url, data=data, auth=auth, timeout=timeout))
        return SimpleNamespace(status_code=self.status, json=lambda: self.body, text="")


@pytest.fixture
def twilio(monkeypatch):
    for name, value in (("TWILIO_ACCOUNT_SID", "ACtest"), ("TWILIO_AUTH_TOKEN", SECRET),
                        ("TWILIO_FROM_NUMBER", "+15550001111"), ("ONCALL_PRIMARY_PHONE", NUMBER)):
        monkeypatch.setattr(config, name, value)


def event(severity="SEV1 (critical service)", subject="[on-call SEV1] INC-9: fix"):
    return {"subject": subject, "severity": severity, "title": "no allowlisted fix",
            "lines": ["Affected services: payment, checkout"]}


def test_places_a_call_and_never_records_the_full_number(twilio):
    http = FakeHttp()
    result = notify.CallNotifier(http=http).notify(event())
    call = http.calls[0]
    assert call.url == "https://api.twilio.com/2010-04-01/Accounts/ACtest/Calls.json"
    assert call.auth == ("ACtest", SECRET) and call.data["To"] == NUMBER and call.timeout == 10.0
    spoken = unquote_plus(call.data["Url"])
    assert "Twiml" not in call.data and spoken.startswith("https://twimlets.com/message?")
    assert "Severity 1 incident" in spoken and "payment, checkout" in spoken
    assert result["status"] == "sent" and result["message_id"] == "CA123"
    assert NUMBER not in str(result) and result["recipients"] == ["+91******2345"]


def test_only_live_serious_incidents_ring(twilio):
    chan = notify.CallNotifier(http=FakeHttp())
    assert chan.wants(event()) and chan.wants(event(severity="SEV2 (high service)"))
    assert not chan.wants(event(severity="SEV3 (medium service)"))
    assert not chan.wants(event(subject="[TEST] [on-call SEV1] INC-9: fix"))


def test_text_cannot_break_out_of_the_link():
    url = notify.spoken_url("a & b=c")
    assert "a & b" not in url and "b=c" not in url and url.count("&") == 1


def test_missing_settings_fail_without_a_request(twilio, monkeypatch):
    monkeypatch.setattr(config, "TWILIO_FROM_NUMBER", "")
    http = FakeHttp()
    result = notify.CallNotifier(http=http).notify(event())
    assert result["status"] == "failed" and "TWILIO_FROM_NUMBER" in result["error"] and http.calls == []


def test_twilio_refusal_is_reported_without_token_or_number(twilio):
    http = FakeHttp(status=400, body={"message": f"bad {SECRET} for {NUMBER}", "code": 21608})
    result = notify.CallNotifier(http=http).notify(event())
    assert result["status"] == "failed" and "21608" in result["error"]
    assert SECRET not in result["error"] and NUMBER not in result["error"]


def test_network_error_never_raises_or_leaks(twilio):
    class Broken:
        def post(self, *a, **kw):
            raise ConnectionError(f"failed with {SECRET}")
    result = notify.CallNotifier(http=Broken()).notify(event())
    assert result["status"] == "failed" and SECRET not in result["error"]


def test_calls_are_off_by_default_and_on_when_chosen(twilio, monkeypatch):
    monkeypatch.setattr(config, "EMAIL_ENABLED", False)
    monkeypatch.setattr(config, "CALLS_ENABLED", False)
    assert [c.channel for c in notify.notifiers()] == ["log"]
    monkeypatch.setattr(config, "CALLS_ENABLED", True)
    assert [c.channel for c in notify.notifiers()] == ["log", "call"]


def test_a_fixture_run_never_rings(twilio, monkeypatch):
    http = FakeHttp()
    chan = notify.CallNotifier(http=http)
    record = {"metadata": {"run_id": "R", "incident_id": "I", "mode": "test"}, "diagnosis": {"confidence": 0.1},
              "investigation": {"outcome": "llm_error"}, "escalations": [], "outcome": {}, "report_paths": {}}
    notify.dispatch(record, chans=[chan], log=lambda line: None)
    assert http.calls == []


def test_test_call_says_it_is_a_test(twilio, monkeypatch):
    http = FakeHttp()
    real = notify.CallNotifier
    monkeypatch.setattr(notify, "CallNotifier", lambda: real(http=http))
    assert notify.send_test_call()["status"] == "sent"
    assert "only a test call" in unquote_plus(http.calls[0].data["Url"])
