"""The incident record (runs/<run_id>.json): complete, schema-valid, written incrementally and
atomically, scrubbed, indexed, and the single source for the terminal and the report."""
import json
import os
import re
from types import SimpleNamespace

import pytest

from oncall import config
from oncall.action_api import FakeClient
from oncall.agent import run
from oncall.evidence import registry
from oncall.record import FLAG_TEXT, atomic_write, estimate_cost, upgrade
from oncall.report import build_report, render_outcome, to_organiser_schema
from oncall.verify import RECOVERED
from tests.schema_check import errors
from tests.test_agent_loop import block_tool, make_agent, response
from tests.test_remediate import StubVerifier, human

ALERT = {"incident_id": "INC-7", "service": "checkout", "signal": "error_rate", "value": 0.95}


def diagnosis_script(n, kwargs):
    if n == 1:
        return response(block_tool("d", "get_deploy_history", {"lookback": "30m"}))
    eid = json.loads(kwargs["messages"][-1]["content"][0]["content"])["changed_or_unhealthy"][0]["evidence"]
    return response(block_tool("s", "submit_diagnosis", {
        "headline": "payment is down, checkout fails",
        "affected_services": [{"service": "checkout", "impact": "orders fail", "evidence": [eid]}],
        "anomalies": [],
        "root_causes": [{"component": "payment", "fault_class": "container_stopped_or_restarting",
                         "cause": "payment container exited", "confidence": 0.9, "evidence": [eid, "E999"]}],
        "ruled_out": [{"service": "cart", "reason": "cart is running and healthy", "evidence": [eid]}],
        # the model names the victim; the validator retargets the root cause (an override)
        "proposed_action": {"action": "restart_service", "target": "checkout", "reason": "restart it"},
        "prevention": ["Alert on payment container exits."],
        "open_questions": ["Why did payment exit?"]}))


def fake_run(monkeypatch, script=diagnosis_script, approve=None, client=None, verifier=None, alert=ALERT):
    agent, _ = make_agent(monkeypatch, script)
    agent.action_client = client or FakeClient()
    agent.approve = approve or human({})
    agent.verifier = verifier or StubVerifier({"payment": RECOVERED})
    return agent.run(dict(alert))


def record_files():
    return sorted(p for p in config.RUNS_DIR.glob("*.json"))


def index_lines():
    path = config.RUNS_DIR / "index.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_full_fake_run_is_complete_and_schema_valid(payment_down, monkeypatch):
    rec = fake_run(monkeypatch)
    data = json.loads(open(rec["path"], encoding="utf-8").read())
    assert errors(data) == [] and data["status"] == "complete"
    assert {k: v for k, v in rec.items() if k != "path"} == data   # returned == saved

    meta = data["metadata"]
    assert (meta["incident_id"], meta["mode"], meta["provider"], meta["model"]) == \
        ("INC-7", "test", "anthropic", "claude-opus-5-5")
    assert meta["started_at"] and meta["ended_at"] and set(meta["git"]) == {"commit", "dirty"}
    assert data["alert"] == ALERT                                   # exactly as received

    step = data["investigation"]["steps"][0]
    assert step["tool"] == "get_deploy_history" and step["arguments"] == {"lookback": "30m"}
    assert step["evidence_ids"] and step["error"] is None and step["seconds"] is not None
    ids = {e["id"] for e in data["evidence"]}
    assert set(step["evidence_ids"]) <= ids
    cited = {e["id"] for e in data["evidence"] if e["cited"]}
    assert cited == set(data["diagnosis"]["root_causes"][0]["evidence"])

    d = data["diagnosis"]
    assert d["ruled_out"][0]["service"] == "cart" and d["open_questions"] == ["Why did payment exit?"]
    assert d["validation"]["dropped_evidence_ids"] == ["E999"]
    override = d["validation"]["overrides"][0]
    assert override["proposed"]["target"] == "checkout" and override["applied"]["target"] == "payment"

    s = data["remediation_plan"]["steps"][0]
    assert (s["action"], s["target"], s["params"], s["outcome"]) == ("restart_service", "payment", {}, "recovered")
    assert s["approval"]["approved_by"] == "human"
    assert s["kill_switch"] == {"before_approval": "off", "before_action": "off"}
    assert s["execution"]["status"] == "executed" and s["execution"]["request"]["json"] == {"service": "payment"}
    assert s["verify"]["outcome"] == "recovered"

    assert data["outcome"]["status"] == "recovered" and "payment" in data["outcome"]["summary"]
    assert data["outcome"]["prevention"] == ["Alert on payment container exits."]
    acc = data["accounting"]
    assert (acc["tokens_in"], acc["tokens_out"], acc["steps_used"], acc["step_budget"]) == (20, 10, 2, 10)
    assert acc["estimated_cost_usd"] == estimate_cost("claude-opus-5-5", 20, 10)
    assert acc["phases"]["investigation_s"] is not None and acc["phases"]["total_s"] is not None

    paths = data["report_paths"]
    assert os.path.exists(paths["report"])
    log = config.ACTION_LOG.read_text().splitlines()
    assert paths["action_log_lines"] and all(
        json.loads(log[n - 1])["run_id"] == meta["run_id"] for n in paths["action_log_lines"])


def test_interrupted_investigation_leaves_a_valid_partial_file(payment_down, monkeypatch):
    def script(n, kwargs):
        if n == 1:
            return response(block_tool("d", "get_deploy_history", {"lookback": "30m"}))
        raise KeyboardInterrupt  # Ctrl+C while waiting for the model

    with pytest.raises(KeyboardInterrupt):
        fake_run(monkeypatch, script=script)
    [path] = record_files()
    data = json.loads(path.read_text())
    assert errors(data) == [] and data["status"] == "interrupted"
    assert len(data["investigation"]["steps"]) == 1 and data["diagnosis"] is None and data["outcome"] is None
    assert index_lines() == []  # only finished runs are indexed
    assert not list(config.RUNS_DIR.glob("*.tmp"))


def test_interrupted_plan_keeps_the_diagnosis(payment_down, monkeypatch):
    def approve(step, diag):
        raise KeyboardInterrupt  # Ctrl+C at the approval prompt of a custom approver

    with pytest.raises(KeyboardInterrupt):
        fake_run(monkeypatch, approve=approve)
    [path] = record_files()
    data = json.loads(path.read_text())
    assert errors(data) == [] and data["status"] == "interrupted"
    assert data["diagnosis"]["status"] == "diagnosed" and data["remediation_plan"]["steps"]
    assert path.with_suffix(".report.md").exists()


def test_index_gets_exactly_one_line_per_finished_run(payment_down, monkeypatch):
    first = fake_run(monkeypatch)
    second = fake_run(monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        fake_run(monkeypatch, approve=lambda s, d: (_ for _ in ()).throw(KeyboardInterrupt()))
    lines = index_lines()
    assert [line["run_id"] for line in lines] == [first["metadata"]["run_id"], second["metadata"]["run_id"]]
    assert first["metadata"]["run_id"] != second["metadata"]["run_id"]  # same second, distinct files
    assert lines[0] == {"run_id": first["metadata"]["run_id"], "incident_id": "INC-7",
                        "time": first["metadata"]["ended_at"], "mode": "test",
                        "fault_class": "container_stopped_or_restarting", "component": "payment",
                        "confidence": 0.9, "status": "recovered",
                        "cost_usd": first["accounting"]["estimated_cost_usd"]}


SECRETS = ["sk-ant-api03-SECRETSECRETSECRET", "tok-VERY-SECRET-123", "Bearer abcdef123456", "OTHERSECRET99"]


def test_no_secret_or_flag_data_is_saved(payment_down, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRETS[0])
    monkeypatch.setattr(config, "ACTION_API_TOKEN", SECRETS[1])
    alert = {**ALERT, "note": "feature_flag.paymentFailure=on",
             "headers": {"Authorization": SECRETS[2], "x-api-key": "sk-ant-api03-OTHERSECRET99"},
             "comment": f"token is {SECRETS[1]}", "feature_flag_state": {"paymentFailure": True}}
    real_dispatch = run.dispatch

    def leaky_dispatch(name, args):
        result, is_error = real_dispatch(name, args)
        registry.add(name, "log", "payment log: feature_flag paymentFailure evaluated to true")
        return {**result, "extra": "feature_flag: paymentFailure", "api_key": SECRETS[0],
                "lines": "ok line\nfeature_flag.x = on\nanother ok line"}, is_error

    monkeypatch.setattr(run, "dispatch", leaky_dispatch)
    rec = fake_run(monkeypatch, alert=alert)
    texts = {"record": open(rec["path"], encoding="utf-8").read(),
             "report": open(rec["report_paths"]["report"], encoding="utf-8").read(),
             "index": (config.RUNS_DIR / "index.jsonl").read_text(),
             "actions": config.ACTION_LOG.read_text()}
    for name, text in texts.items():
        for secret in SECRETS:
            assert secret not in text, (name, secret)
        assert "sk-ant-" not in text and not FLAG_TEXT.search(text), name
    data = json.loads(texts["record"])
    assert errors(data) == []
    assert data["alert"]["headers"] == {"Authorization": "<redacted>", "x-api-key": "<redacted>"}
    assert "feature_flag_state" not in data["alert"]
    assert data["investigation"]["steps"][0]["result"]["lines"] == "ok line\nanother ok line"


def test_report_and_terminal_are_rendered_from_the_json(payment_down, monkeypatch, capsys):
    rec = fake_run(monkeypatch)
    terminal = capsys.readouterr().out
    data = json.loads(open(rec["path"], encoding="utf-8").read())
    report = open(rec["report_paths"]["report"], encoding="utf-8").read()
    # the same functions over the saved file reproduce exactly what was shown and written
    assert render_outcome(data) in terminal and build_report(data) == report

    d, out, acc, plan = data["diagnosis"], data["outcome"], data["accounting"], data["remediation_plan"]
    cost = f"${acc['estimated_cost_usd']:.4f}"
    for text in (terminal, report):
        assert out["summary"] in text and cost in text
        assert f"{d['root_causes'][0]['component']} [{d['root_causes'][0]['fault_class']}]" in text
    assert f"=== OUTCOME [{out['status']}]" in terminal and f"**Outcome: {out['status']}.**" in report
    assert f"DIAGNOSIS [{d['status']}]" in terminal and f"Diagnosis: **{d['status']}**" in report
    assert f"Overall Confidence: {d['confidence']:.2f}" in terminal and f"confidence {d['confidence']:.2f}" in report
    assert f"plan outcome: {plan['outcome']}" in terminal and f"Plan: **{plan['outcome']}**" in report
    step = plan["steps"][0]
    assert f"step 1 {step['action']} {step['target']}: {step['outcome']}" in terminal
    assert f"Step outcome: **{step['outcome']}**" in report


def test_schema_checker_rejects_broken_records(payment_down, monkeypatch):
    data = json.loads(open(fake_run(monkeypatch)["path"], encoding="utf-8").read())
    broken = json.loads(json.dumps(data))
    del broken["metadata"]["run_id"]
    broken["outcome"]["status"] = "fine"
    broken["remediation_plan"]["steps"][0]["action"] = "docker_restart"
    problems = errors(broken)
    assert any("missing 'run_id'" in p for p in problems)
    assert any("$.outcome.status" in p for p in problems)
    assert any("$.remediation_plan.steps[0].action" in p for p in problems)


def test_day1_records_upgrade_to_valid_records():
    root = config.ROOT / "results" / "checkpoint2"
    for path in sorted(root.glob("*.json")):
        data = upgrade(json.loads(path.read_text(encoding="utf-8")))
        assert errors(data) == [], (path.name, errors(data)[:3])
        assert "## Root cause" in build_report(data) and to_organiser_schema(data)["root_causes"]


def test_atomic_write_survives_a_busy_target(tmp_path, monkeypatch):
    real, calls = os.replace, []

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) < 3:
            raise PermissionError("file in use")  # e.g. open in an editor on Windows
        real(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    monkeypatch.setattr("oncall.record.time.sleep", lambda s: None)
    target = tmp_path / "r.json"
    atomic_write(target, '{"ok": true}')
    assert json.loads(target.read_text()) == {"ok": True} and len(calls) == 3
    assert [p.name for p in tmp_path.iterdir()] == ["r.json"]  # no temp file left behind


def test_verify_only_keeps_the_record_valid_and_the_index_unchanged(payment_down, monkeypatch):
    from oncall import __main__ as cli
    from oncall import verify

    rec = fake_run(monkeypatch, client=FakeClient(live=False))  # a dry run: nothing executed
    assert rec["outcome"]["status"] == "escalated"

    class Recovered(StubVerifier):
        def __init__(self, **kw):
            super().__init__({"payment": RECOVERED})

    monkeypatch.setattr(verify, "Verifier", Recovered)
    cli.cmd_verify(SimpleNamespace(run=rec["path"], timeout=None))
    data = json.loads(open(rec["path"], encoding="utf-8").read())
    assert errors(data) == [] and data["outcome"]["status"] == "recovered"
    assert data["manual_verifications"][0]["outcome"] == "recovered"
    assert len(index_lines()) == 1
    assert re.search(r"Manual remediation verified: recovered", open(rec["report_paths"]["report"]).read())
