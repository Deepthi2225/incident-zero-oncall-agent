"""What an incident email looks like: [TEST] marking, HTML part, attached report, scrubbing."""
import base64
import email
from types import SimpleNamespace

from oncall import config, notify


def record(tmp_path, mode="live", cause="payment exited", report="# Report\nall the details\n"):
    path = tmp_path / "run.report.md"
    if report is not None:
        path.write_text(report, encoding="utf-8")
    return {"metadata": {"run_id": "R1", "incident_id": "INC-9", "mode": mode},
            "diagnosis": {"confidence": 0.9, "affected_services": ["payment"], "open_questions": [],
                          "root_causes": [{"component": "payment", "fault_class": "service_error", "cause": cause,
                                           "confidence": 0.9, "evidence": ["E1"]}]},
            "evidence": [{"id": "E1", "tool": "find_error_traces", "observation": "deepest span is payment", "cited": True}],
            "escalations": [], "outcome": {"summary": "escalated"}, "report_paths": {"report": str(path)}}


def event(rec):
    return notify.build_event(rec, "low_confidence", "confidence is low")


def test_live_incident_has_no_test_marking(tmp_path):
    e = event(record(tmp_path))
    assert e["subject"].startswith("[on-call") and "TEST ONLY" not in notify.body_text(e)


def test_fixture_and_replay_runs_are_marked(tmp_path):
    for mode, words in (("test", "fixture data"), ("bundle", "a recorded replay")):
        e = event(record(tmp_path, mode=mode))
        assert e["subject"].startswith("[TEST] ")
        assert words in e["lines"][0] and "TEST ONLY" in notify.email_html(e)
        assert e["attachment"][1].startswith("*** TEST ONLY")


def test_html_layout_and_readable_evidence(tmp_path):
    page = notify.email_html(event(record(tmp_path)))
    assert "low confidence, needs a human" in page and "INC-9" in page
    assert "<h3" in page and "<table" in page and "#" in page
    assert "<strong>Traces</strong>" in page and "find_error_traces" not in page


def test_html_escapes_everything(tmp_path):
    page = notify.email_html(event(record(tmp_path, cause='<img src=x onerror="alert(1)">')))
    assert "<img" not in page and "&lt;img" in page


def test_secrets_do_not_reach_html_or_attachment(tmp_path, monkeypatch):
    secret = "tok-VERY-SECRET-123"
    monkeypatch.setattr(config, "ACTION_API_TOKEN", secret)
    e = event(record(tmp_path, cause=f"leaked {secret}", report=f"token {secret}\n"))
    assert secret not in notify.email_html(e) and secret not in e["attachment"][1]


def test_smtp_message_has_plain_html_and_report(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EMAIL_TO", ["a@example.test"])
    raw = notify.EmailNotifier().message(event(record(tmp_path))).as_bytes()
    parts = list(email.message_from_bytes(raw).walk())
    assert {"text/plain", "text/html"} <= {p.get_content_type() for p in parts}
    report = next(p for p in parts if p.get_filename())
    assert report.get_filename() == "incident-report-R1.txt"
    assert "all the details" in report.get_payload(decode=True).decode()


def test_resend_body_has_html_and_report(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RESEND_API_KEY", "not-a-real-key")
    _, body = notify.ResendNotifier(http=object()).request(event(record(tmp_path)))
    assert body["html"].startswith("<!doctype html>")
    assert body["attachments"][0]["filename"] == "incident-report-R1.txt"
    assert "all the details" in base64.b64decode(body["attachments"][0]["content"]).decode()


def test_missing_report_says_where_it_is_and_still_sends(tmp_path):
    e = event(record(tmp_path, report=None))
    assert e["attachment"] is None
    assert any(line.startswith("Report: on the agent's machine") for line in e["lines"])


def test_smtp_gives_the_host_name_that_starttls_verifies(tmp_path, monkeypatch):
    seen = {}

    class FakeSMTP:
        def __init__(self, host="", port=0, timeout=None):
            seen.update(host=host, port=port)
            self.sock = SimpleNamespace(settimeout=lambda t: None)

        def starttls(self, context=None):
            seen["starttls"] = True

        def ehlo(self):
            pass

        def login(self, user, password):
            pass

        def send_message(self, msg):
            seen["sent"] = True

        def quit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    for name, value in (("SMTP_HOST", "smtp.example.test"), ("SMTP_PORT", 587), ("SMTP_STARTTLS", True),
                        ("SMTP_USER", "u"), ("SMTP_PASSWORD", "p"), ("EMAIL_TO", ["a@example.test"])):
        monkeypatch.setattr(config, name, value)
    assert notify.EmailNotifier().notify(event(record(tmp_path)))["status"] == "sent"
    assert seen == {"host": "smtp.example.test", "port": 587, "starttls": True, "sent": True}
