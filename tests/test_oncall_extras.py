"""Rotation, acknowledge-or-escalate, Slack and incident memory."""
import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from oncall import ack, config, memory, notify
from oncall.record import append_line

PLAN = {"people": {"a": {"email": "a@x.test"}, "b": {"email": "b@x.test"}, "boss": {"email": "boss@x.test"}},
        "rotations": {"week": {"people": ["a", "b"], "start": "2026-11-02", "days": 7}},
        "acknowledge": {"wait_minutes": 5, "then": ["boss"]},
        "routes": {"SEV1": ["@week"]}}
START = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def plan(tmp_path, monkeypatch):
    path = tmp_path / "escalation.json"
    path.write_text(json.dumps(PLAN), encoding="utf-8")
    monkeypatch.setattr(config, "ESCALATION_FILE", path)
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    monkeypatch.setenv("ONCALL_NOTIFY_LOG", str(tmp_path / "notifications.jsonl"))
    return path


class FakeChannel(notify.Notifier):
    channel, contact = "email", "email"

    def __init__(self, status="sent"):
        self.status, self.to, self.sent = status, [], []

    def use(self, people):
        self.to = people

    def notify(self, event):
        self.sent.append((list(self.to), event))
        return {"channel": "email", "status": self.status, "error": "nope"}


def shared(chan):  # copies made for routing share the same sent list
    return chan.sent


def alerted(incident="INC-1", severity="SEV1 (critical service)", subject="[on-call SEV1] INC-1: x", at=START):
    append_line(notify.notify_log(), {"ts": at.isoformat(), "channel": "log", "status": "logged", "incident_id": incident,
                                      "event": "low_confidence", "severity": severity, "subject": subject})


# --- rotation -------------------------------------------------------------------------------------------

def test_rotation_takes_turns_by_week(plan):
    assert notify.on_call("week", PLAN, date(2026, 11, 2)) == "a"
    assert notify.on_call("week", PLAN, date(2026, 11, 8)) == "a"
    assert notify.on_call("week", PLAN, date(2026, 11, 9)) == "b"
    assert notify.on_call("week", PLAN, date(2026, 11, 16)) == "a"


def test_a_route_can_name_a_rotation(plan, monkeypatch):
    monkeypatch.setattr(notify, "on_call", lambda name, p=None, today=None: "b")
    assert notify.contacts_for({"subject": "x", "severity": "SEV1"}, "email") == ["b@x.test"]


@pytest.mark.parametrize("rotations", [{}, {"week": {"people": [], "start": "2026-11-02"}},
                                       {"week": {"people": ["a"], "start": "soon"}},
                                       {"week": {"people": ["a"], "start": "2026-11-02", "days": 0}}])
def test_a_broken_rotation_falls_back_to_env_contacts(plan, rotations):
    assert notify.contacts_of(["@week"], "email", {**PLAN, "rotations": rotations}) == []


# --- acknowledge or escalate -----------------------------------------------------------------------------

def test_overdue_incident_escalates_once(plan):
    alerted()
    chan = FakeChannel()
    late = START + timedelta(minutes=6)
    assert ack.escalate_overdue(log=lambda s: None, chans=[chan], now=late) == ["INC-1"]
    assert shared(chan)[0][0] == ["boss@x.test"] and "ack INC-1" in " ".join(shared(chan)[0][1]["lines"])
    assert ack.escalate_overdue(log=lambda s: None, chans=[chan], now=late) == [] and len(shared(chan)) == 1


def test_not_overdue_yet(plan):
    alerted()
    assert ack.escalate_overdue(log=lambda s: None, chans=[FakeChannel()], now=START + timedelta(minutes=4)) == []


def test_acknowledged_incident_does_not_escalate(plan):
    alerted()
    ack.acknowledge("INC-1")
    ack.acknowledge("INC-1")  # repeating is safe
    assert (config.RUNS_DIR / "acks.jsonl").read_text().count("INC-1") == 1
    assert ack.escalate_overdue(log=lambda s: None, chans=[FakeChannel()], now=START + timedelta(hours=1)) == []


@pytest.mark.parametrize("kwargs", [{"subject": "[TEST] [on-call SEV1] INC-1: x"}, {"severity": "SEV3 (medium service)"}])
def test_test_runs_and_minor_incidents_do_not_escalate(plan, kwargs):
    alerted(**kwargs)
    assert ack.escalate_overdue(log=lambda s: None, chans=[FakeChannel()], now=START + timedelta(hours=1)) == []


def test_a_failed_send_tries_again_on_the_next_poll(plan):
    alerted()
    late = START + timedelta(minutes=6)
    assert ack.escalate_overdue(log=lambda s: None, chans=[FakeChannel("failed")], now=late) == []
    assert ack.escalate_overdue(log=lambda s: None, chans=[FakeChannel()], now=late) == ["INC-1"]


def test_no_acknowledge_rule_means_no_escalation(plan):
    plan.write_text(json.dumps({"routes": {}}), encoding="utf-8")
    alerted()
    assert ack.escalate_overdue(log=lambda s: None, chans=[FakeChannel()], now=START + timedelta(hours=1)) == []


# --- slack ---------------------------------------------------------------------------------------------

URL = "https://hooks.slack.test/services/SECRETSECRET"


class FakeHttp:
    def __init__(self, status=200):
        self.status, self.calls = status, []

    def post(self, url, json=None, timeout=None):
        self.calls.append(SimpleNamespace(url=url, json=json))
        return SimpleNamespace(status_code=self.status)


EVENT = {"subject": "[on-call SEV1] INC: x", "severity": "SEV1", "title": "x", "lines": ["Affected services: payment"]}


def test_slack_posts_the_alert_and_keeps_the_url_out_of_results(monkeypatch):
    monkeypatch.setattr(config, "SLACK_WEBHOOK_URL", URL)
    http = FakeHttp()
    result = notify.SlackNotifier(http=http).notify(EVENT)
    assert http.calls[0].url == URL and "payment" in http.calls[0].json["text"]
    assert result["status"] == "sent" and URL not in str(result)


def test_slack_failures_never_leak_the_url(monkeypatch):
    monkeypatch.setattr(config, "SLACK_WEBHOOK_URL", URL)

    class Broken:
        def post(self, *a, **k):
            raise ConnectionError(f"failed for {URL}")
    assert URL not in notify.SlackNotifier(http=Broken()).notify(EVENT)["error"]
    assert "HTTP 403" in notify.SlackNotifier(http=FakeHttp(403)).notify(EVENT)["error"]


def test_slack_skips_test_runs_and_is_off_by_default(monkeypatch):
    monkeypatch.setattr(config, "EMAIL_ENABLED", False)
    assert "slack" not in [c.channel for c in notify.notifiers()]
    assert not notify.SlackNotifier(http=FakeHttp()).wants({"subject": "[TEST] x"})


# --- memory ---------------------------------------------------------------------------------------------

def row(component, mode="live", status="recovered"):
    return {"time": "2026-11-01", "mode": mode, "component": component, "fault_class": "service_error",
            "confidence": 0.9, "status": status}


def remember(tmp_path, monkeypatch, *rows):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    for r in rows:
        append_line(tmp_path / "index.jsonl", r)


def test_memory_finds_the_service_and_what_it_depends_on(tmp_path, monkeypatch):
    remember(tmp_path, monkeypatch, row("payment"), row("ad"))
    assert [r["component"] for r in memory.similar({"alerting_services": ["checkout"]})] == ["payment"]


def test_memory_ignores_test_runs_and_keeps_the_newest(tmp_path, monkeypatch):
    remember(tmp_path, monkeypatch, row("payment", mode="test"), *[row("payment", status=f"s{i}") for i in range(5)])
    found = memory.similar({"service": "payment"})
    assert [r["status"] for r in found] == ["s4", "s3", "s2"]


def test_memory_says_it_is_not_evidence(tmp_path, monkeypatch):
    remember(tmp_path, monkeypatch, row("payment"))
    assert "NOT evidence" in memory.note({"service": "payment"})
    assert memory.note({"service": "quote"}) == ""


def test_memory_with_no_history_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    assert memory.note({"service": "payment"}) == ""
