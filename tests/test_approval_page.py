"""The "approval needed" page: sent the moment a step's approval box is shown in the terminal,
once per step (also across a restart), in the background so the prompt is not delayed, never
blocking the approval, scrubbed, and recorded in the incident record and notifications.jsonl.
Fake transports only; the developer's .env is never read (tests/conftest.py)."""
from tests.mailtools import plain_text
import json
import sys
import time
from email.header import decode_header, make_header

import pytest

from oncall import config, notify
from oncall.action_api import FakeClient
from oncall.verify import RECOVERED
from tests.schema_check import errors
from tests.test_agent_loop import make_agent
from tests.test_incident_record import ALERT, diagnosis_script
from tests.test_notify import edited, smtp  # noqa: F401  (smtp is a fixture)
from tests.test_notify_resend import resend  # noqa: F401  (a fixture)
from tests.test_remediate import StubVerifier

SECRET = "sk-ant-api03-PAGESECRET1234567890"
SUBJECT = "[TEST] [ON-CALL PAGE][SEV1] INC-7 payment: approval needed — restart_service on payment"


def run_paged(monkeypatch, script=diagnosis_script, answer="y", alert=ALERT, **agent_kw):
    """A run with the agent's own approver, at a (simulated) terminal where a human types answer."""
    agent, _ = make_agent(monkeypatch, script, **agent_kw)
    agent.action_client = FakeClient(live=False)
    agent.verifier = StubVerifier({"payment": RECOVERED, "valkey-cart": RECOVERED})
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": answer)
    rec = agent.run(dict(alert))
    assert errors({k: v for k, v in rec.items() if k != "path"}) == []
    return rec


def pages(server):
    return [m["msg"] for m in server.messages if m["msg"]["Subject"].startswith("[TEST] [ON-CALL PAGE]")]


def page_entries(rec):
    return [n for n in rec["notifications"] if n["event"] == "approval_page"]


def subject(msg):
    return str(make_header(decode_header(msg["Subject"])))  # the em dash travels RFC 2047-encoded


def body(msg):
    return plain_text(msg)


def two_causes(inp):  # payment stopped and its cache down: a two-step plan
    rc = dict(inp["root_causes"][0])
    inp["root_causes"].append({**rc, "component": "valkey-cart", "fault_class": "dependency_down",
                               "cause": "valkey-cart is down", "confidence": 0.85})


def test_one_page_per_awaiting_step_and_none_after_a_restart(payment_down, monkeypatch, smtp, capsys):
    server = smtp()
    rec = run_paged(monkeypatch, edited(two_causes))
    steps = rec["remediation_plan"]["steps"]
    assert len(steps) == 2 and all(s["approval"]["status"] == "approved" for s in steps)
    assert len(pages(server)) == 2
    keys = [n["key"] for n in page_entries(rec) if n["channel"] == "email"]
    assert keys == [notify.page_key(s) for s in steps] and all(n["status"] == "sent" for n in page_entries(rec)
                                                               if n["channel"] == "email")
    logged = [json.loads(x) for x in notify.notify_log().read_text(encoding="utf-8").splitlines()]
    assert sum(1 for x in logged if x["event"] == "approval_page" and x["channel"] == "email") == 2

    again = run_paged(monkeypatch, edited(two_causes))            # agent restarted on the same incident
    assert len(pages(server)) == 2 and page_entries(again) == []
    assert "already paged for INC-7, not repeated" in capsys.readouterr().out


def test_page_content(payment_down, monkeypatch, smtp):
    server = smtp()

    def leaky(inp):
        inp["root_causes"][0]["cause"] = f"payment exited (env had {SECRET})"
    run_paged(monkeypatch, edited(leaky))
    msg = pages(server)[0]
    assert subject(msg) == SUBJECT
    text = body(msg)
    assert text.startswith("*** TEST ONLY: this came from fixture data")
    assert "This is an on-call page from Incident Zero. A fix is waiting for your approval in the terminal." in text
    for part in ("Incident: INC-7", "Severity: SEV1 (critical service)",
                 "Root cause: payment [container_stopped_or_restarting]: payment exited (env had <redacted>)",
                 "Confidence: 0.9", "Reviewer: did not run", "Proposed action: restart_service on payment (step 1)",
                 "Reason: ", "Expected outcome: ", "Risk: ", "Rollback: ", "Top evidence:", "  - E1 [get_deploy_history]",
                 "Where to approve: in the terminal running the agent", "http://127.0.0.1:8090",
                 "Approval is NOT possible by replying to this email."):
        assert part in text, part
    assert SECRET not in str(msg) + text


def test_reviewer_verdict_and_objections_are_in_the_page(payment_down, monkeypatch):
    record = {"metadata": {"incident_id": "INC-R", "run_id": "r"},
              "diagnosis": {"confidence": 0.7, "root_causes": [{"component": "cart", "fault_class": "dependency_down",
                                                                "cause": "valkey down", "confidence": 0.7,
                                                                "evidence": ["E2"]}]},
              "review": {"status": "done", "verdict": "doubt", "confidence_before": 0.8, "confidence_after": 0.7,
                         "objections": [{"text": "errors began before the cache restart", "evidence": ["E2"]}]},
              "evidence": [{"id": "E2", "tool": "search_logs", "observation": "cart: connection refused", "cited": True}]}
    step = {"step": 1, "action": "restart_service", "target": "valkey-cart", "component": "cart", "reason": "r",
            "rollback": None}
    page = notify.build_page(record, step)
    assert page["subject"] == "[ON-CALL PAGE][SEV2] INC-R cart: approval needed — restart_service on valkey-cart"
    assert "Reviewer: doubt (confidence 0.8 -> 0.7)" in page["lines"]
    assert "  objection: errors began before the cache restart (E2)" in page["lines"]
    assert "Rollback: none (cannot be undone)" in page["lines"]


def test_the_prompt_is_not_delayed_by_a_hanging_mail_server(payment_down, monkeypatch, smtp):
    smtp("hang", timeout=3)
    marks = {}
    real_page = notify.page

    def timed_page(*a, **k):
        marks["page"] = time.monotonic()
        return real_page(*a, **k)
    monkeypatch.setattr(notify, "page", timed_page)
    agent, _ = make_agent(monkeypatch, diagnosis_script)
    agent.action_client, agent.verifier = FakeClient(live=False), StubVerifier({"payment": RECOVERED})
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": marks.setdefault("prompt", time.monotonic()) and "y")
    rec = agent.run(dict(ALERT))
    assert marks["prompt"] - marks["page"] < 0.5                      # the box did not wait for the server
    step = rec["remediation_plan"]["steps"][0]
    assert step["approval"]["status"] == "approved" and step["execution"]["status"] == "dry_run"
    email = [n for n in page_entries(rec) if n["channel"] == "email"][0]
    assert email["status"] == "failed" and email["error"].startswith("email failed: ")  # recorded, not lost


def test_a_failed_page_does_not_block_the_approval(payment_down, monkeypatch, smtp, capsys):
    smtp("reject")
    rec = run_paged(monkeypatch)
    step = rec["remediation_plan"]["steps"][0]
    assert step["approval"]["status"] == "approved" and step["outcome"] == "dry_run"
    email = [n for n in page_entries(rec) if n["channel"] == "email"][0]
    assert email["status"] == "failed" and "554" in email["error"]
    assert "[notify] approval_page: email failed: " in capsys.readouterr().out


def test_a_rejected_step_was_still_paged_once(payment_down, monkeypatch, smtp):
    server = smtp()
    rec = run_paged(monkeypatch, answer="n")
    assert len(pages(server)) == 1                                    # the page, then the human said no
    assert [n["event"] for n in rec["notifications"] if n["channel"] == "email"] == ["approval_page", "human_rejected"]


@pytest.mark.parametrize("how", ["auto_approve", "no_terminal", "page_off"])
def test_no_page_when_nobody_is_asked_or_it_is_off(payment_down, monkeypatch, smtp, how):
    server = smtp()
    if how == "page_off":
        monkeypatch.setattr(config, "EMAIL_ON_APPROVAL", False)
        rec = run_paged(monkeypatch)
    elif how == "auto_approve":
        rec = run_paged(monkeypatch, auto_approve=True)
    else:
        agent, _ = make_agent(monkeypatch, diagnosis_script)
        agent.action_client, agent.verifier = FakeClient(live=False), StubVerifier({"payment": RECOVERED})
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
        rec = agent.run(dict(ALERT))
    assert pages(server) == [] and page_entries(rec) == []


def test_page_through_resend(payment_down, monkeypatch, resend):
    fake = resend()
    rec = run_paged(monkeypatch)
    call = fake.calls[0]
    assert call["json"]["subject"] == SUBJECT and "NOT possible by replying" in call["json"]["text"]
    email = [n for n in page_entries(rec) if n["channel"] == "email"][0]
    assert (email["status"], email["transport"]) == ("sent", "resend") and email["message_id"]
