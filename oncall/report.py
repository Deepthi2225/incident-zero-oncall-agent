"""Everything shown to people is rendered FROM the incident record (runs/<run_id>.json):

    render_diagnosis(record)  terminal: the diagnosis and the proposed plan (before approvals)
    render_outcome(record)    terminal: the final outcome, per-step results and accounting
    build_report(record)      markdown: timeline, root cause, evidence, actions with their verify
                              checks and before/after metrics, escalations, prevention

so the terminal, the report and the JSON always agree. to_organiser_schema() maps the same record
to the organisers' format once it is known.

CLI: python -m oncall.report runs/<run_id>.json [--out report.md]
"""
import argparse
import json
from pathlib import Path

from oncall.record import load, upgrade


def _fmt_conf(x):
    return "-" if x is None else f"{x:.2f}"


def _fmt_cost(x):
    return "unknown" if x is None else f"${x:.4f}"


def _fmt(row):
    if not row or not isinstance(row, dict) or "rps" not in row:
        return "-"
    p95 = f", p95 {row['p95_ms']}ms" if row.get("p95_ms") is not None else ""
    return f"rps {row.get('rps')}, err {(row.get('error_rate') or 0) * 100:.1f}%{p95}"


# --- terminal ----------------------------------------------------------------------------------------

def render_diagnosis(record):
    record = upgrade(record)
    meta, d = record["metadata"], record.get("diagnosis") or {}
    plan = record.get("remediation_plan") or {"steps": []}
    lines = [f"\n--- DIAGNOSIS [{d.get('status')}] source={meta['mode']} ({meta.get('source_detail', '')})"]
    if meta["mode"] == "test":
        lines.append("!!! TEST MODE: fixture data, not the live demo")
    lines += [d.get("headline", ""), "\nAffected Services:"]
    lines += [f"  - {a['service']}: {a['impact']} [{', '.join(a['evidence'])}]" for a in d.get("affected_services") or []]
    lines.append("\nObserved Anomalies:")
    lines += [f"  - {a['service']} {a['signal']}: {a['observed']} (baseline {a['baseline']}) [{', '.join(a['evidence'])}]"
              for a in d.get("anomalies") or []]
    rcs = d.get("root_causes") or []
    if rcs:
        lines.append(f"\nRoot Cause(s) [Overall Confidence: {_fmt_conf(d.get('confidence'))}]:")
        for rc in rcs:
            state = ", RESOLVED before action" if rc.get("status") == "resolved" else ""
            lines.append(f"  * {rc['component']} [{rc['fault_class']}] (confidence {_fmt_conf(rc['confidence'])}{state}): {rc['cause']}")
            lines += [f"      {e}: {text[:140]}" for e, text in (rc.get("evidence_detail") or {}).items()]
    else:
        o = d.get("suspected_origin") or {}
        lines.append(f"\nSuspected origin: {o.get('service')} (confidence {o.get('confidence', 0.0)}) - {o.get('reasoning', '')}")
    for r in d.get("ruled_out") or []:
        lines.append(f"  ruled out: {r['service']} - {r['reason']} [{', '.join(r['evidence'])}]")
    lines.append(f"\nRemediation Plan ({len(plan['steps'])} step(s), dependencies first):")
    for s in plan["steps"]:
        lines.append(f"  {s['step']}. {s['action']} on {s['target']} [{s.get('fault_class')}]: {s.get('reason')}")
        if s.get("rollback"):
            lines.append(f"     Rollback: {s['rollback']['action']} ({s['rollback']['note']})")
    for e in record.get("escalations") or []:
        lines.append(f"\nEscalate to human: {e['target']} [{e.get('fault_class')}]: {e['reason']}")
        lines += [f"  Next step: {n}" for n in e.get("next_steps", [])]
    if d.get("prevention"):
        lines += ["\nPrevention:"] + [f"  - {p}" for p in d["prevention"]]
    if d.get("open_questions"):
        lines += ["\nOpen Questions / Next Checks:"] + [f"  - {q}" for q in d["open_questions"]]
    review = record.get("review")
    if review:
        lines.append(f"\nReviewer: {review['reason']}" if review["status"] == "skipped" else
                     f"\nReviewer: {review['verdict']} (confidence {review['confidence_before']} -> "
                     f"{review['confidence_after']})" + ("; ESCALATED" if review["escalated"] else ""))
        lines += [f"  objection: {o['text']} [{', '.join(o['evidence'])}]" for o in review.get("objections", [])[:3]]
    lines += [f"(validator) {n}" for n in (d.get("validation") or {}).get("notes", [])]
    return "\n".join(lines)


def render_outcome(record):
    record = upgrade(record)
    out, acc, plan = record.get("outcome") or {}, record["accounting"], record.get("remediation_plan") or {}
    lines = [f"\n=== OUTCOME [{out.get('status', record['status'])}] {out.get('summary', '')}"]
    for s in plan.get("steps", []):
        v = (s.get("verify") or {}).get("outcome")
        lines.append(f"  step {s['step']} {s['action']} {s['target']}: {s.get('outcome', 'not run')}"
                     + (f" (verify {v})" if v else ""))
    lines.append(f"  plan outcome: {plan.get('outcome')}")
    lines.append(f"  tokens in/out: {acc['tokens_in']}/{acc['tokens_out']}, estimated cost "
                 f"{_fmt_cost(acc.get('estimated_cost_usd'))}, model turns {acc.get('steps_used')}/"
                 f"{acc.get('step_budget')}, total {acc['phases'].get('total_s')}s")
    paths = record.get("report_paths") or {}
    lines.append(f"  report: {paths.get('report')} | action log lines: {paths.get('action_log_lines')}")
    return "\n".join(lines)


# --- markdown report ---------------------------------------------------------------------------------

def _timeline(record):
    alert, rows = record.get("alert") or {}, []
    fired = alert.get("detected_at") or alert.get("fired_at")
    if fired:
        signals = ", ".join(f"{t.get('service')} {t.get('signal')}={t.get('value')}"
                            for t in alert.get("trigger", [])) or \
            f"{alert.get('service')} {alert.get('signal')}={alert.get('value')}"
        rows.append((fired, f"Alert: {signals}"))
    rows.append((record["metadata"].get("started_at") or record["metadata"]["run_id"], "Investigation started"))
    for st in record["investigation"]["steps"]:
        args = ", ".join(f"{k}={v!r}" for k, v in (st.get("arguments") or {}).items())
        when = f"+{st['at_s']}s" if st.get("at_s") is not None else f"step {st.get('step')}"
        rows.append((when, f"{st['tool']}({args}){' FAILED' if st.get('error') else ''}"))
    if record["investigation"].get("submitted_at_s") is not None:
        d = record.get("diagnosis") or {}
        rows.append((f"+{record['investigation']['submitted_at_s']}s",
                     f"Diagnosis: {d.get('status')} (confidence {d.get('confidence')})"))
    for step in (record.get("remediation_plan") or {}).get("steps", []):
        name = f"step {step.get('step')} {step['action']} {step['target']}"
        approval = step.get("approval") or {}
        if approval.get("decision_at"):
            by = f" by {approval['approved_by']}" if approval.get("approved_by") else ""
            rows.append((approval["decision_at"], f"Approval of {name}: {approval.get('status')}{by}"))
        ex = step.get("execution") or {}
        if ex.get("status"):
            why = ex.get("reason") or ex.get("error")
            rows.append((ex.get("at", ""), f"{name}: {ex['status']}" + (f" ({why})" if why else "")))
        v = step.get("verify") or {}
        if v.get("outcome"):
            rows.append(((v.get("checks") or [{}])[-1].get("at", ""), f"{name}: verify {v['outcome']}"
                         + (f" after {len(v.get('checks', []))} check(s)" if v.get("checks") else "")))
        rb = step.get("rollback_result") or {}
        if rb.get("status") and rb["status"] != "none":
            rows.append((rb.get("at", ""), f"{name}: rollback {rb['status']}"))
    for mv in record.get("manual_verifications") or []:
        rows.append((mv["at"], f"Manual remediation verified: {mv['outcome']} "
                     f"({', '.join(t['target'] for t in mv['targets'])})"))
    if record["metadata"].get("ended_at"):
        rows.append((record["metadata"]["ended_at"], f"Run ended: {(record.get('outcome') or {}).get('status')}"))
    return rows


def _verify_lines(v, target, evidence):
    out = []
    if v.get("outcome"):
        out.append(f"- Verify: **{v['outcome']}**" + (f" ({v['reason']})" if v.get("reason") else "")
                   + (f" after {v['seconds']}s" if v.get("seconds") is not None else "")
                   + (f"; {v['note']}" if v.get("note") else ""))
    if v.get("serving"):
        s = v["serving"]
        out.append(f"- Serving again: {s['target']} ({s['server_spans']} successful server span(s), "
                   f"{s['client_calls']} successful call(s) to it since the action; {s.get('evidence')})")
    elif v.get("checks"):
        out.append("- Serving again: no positive evidence (no successful span or call since the action)")
    if v.get("recovering"):
        out.append(f"- Recovering (fast path: container running, server spans succeed) after "
                   f"{v['recovering']['at_s']}s ({v['recovering']['evidence']})")
    if v.get("checks"):
        out += ["", "| Check | Time | Healthy | Passed | Evidence |", "|---|---|---|---|---|"]
        for c in v["checks"]:
            text = evidence.get(c.get("evidence"), {}).get("observation", "").replace("|", "/")
            out.append(f"| {c.get('poll')} | {c.get('at', '')} | {c.get('ok')} | {c.get('passed')}/{c.get('judged')} | "
                       f"{c.get('evidence')}: {text} |")
    if v.get("services"):
        out += ["", "| Service | Baseline | Before | After | OK |", "|---|---|---|---|---|"]
        for svc, m in v["services"].items():
            out.append(f"| {svc} | {_fmt(m.get('baseline'))} | {_fmt(m.get('before'))} | "
                       f"{_fmt(m.get('after'))} | {m.get('ok')} |")
        if v.get("container"):
            c = v["container"]
            out.append(f"\nContainer `{target}`: {c.get('status')}, health {c.get('health')}.")
        if v.get("outcome") == "partial":
            out += ["", "Partial recovery, still unhealthy:"]
            out += [f"- {svc}: {'; '.join(v['services'][svc].get('problems', []))}" for svc in v.get("unhealthy", [])]
        out.append("")
    return out


def _step_section(step, evidence):
    ex, approval = step.get("execution") or {}, step.get("approval") or {}
    why = ex.get("reason") or ex.get("error")
    out = [f"### Step {step.get('step')}: {step['action']} on `{step['target']}`", "",
           f"- Root cause: {step.get('component', step['target'])} [{step.get('fault_class', '?')}]",
           f"- Reason: {step.get('reason', '')}",
           f"- Approval: {approval.get('status', 'not asked')}"
           + (f" by {approval['approved_by']}" if approval.get("approved_by") else ""),
           f"- Risk (as shown to the approver): {approval.get('risk') or step.get('risk_assessment') or 'not assessed'}",
           f"- Kill switch: {step.get('kill_switch') or 'not checked'}",
           f"- Result: {ex.get('status', 'not run')}" + (f" ({why})" if why else ""),
           f"- Step outcome: **{step.get('outcome', 'not run')}**"]
    if ex.get("request"):
        out.append(f"- Request: `{ex['request']['method']} {ex['request']['url']}` `{json.dumps(ex['request']['json'])}`")
    out += _verify_lines(step.get("verify") or {}, step["target"], evidence)
    rb = step.get("rollback_result")
    if rb and rb.get("status") != "none":
        out.append(f"- Rollback: {rb['status']}" + (f" ({rb.get('reason') or rb.get('error')})"
                                                     if rb.get("reason") or rb.get("error") else ""))
    elif step.get("rollback"):
        out.append(f"- Rollback available: {step['rollback']['action']} ({step['rollback']['note']})")
    return out


def _review_section(review):
    """The optional reviewer's second opinion (stretch goal 2)."""
    if not review:
        return []
    out = ["", "## Review", ""]
    if review["status"] == "skipped":
        return out + [f"- {review['reason']}"]
    out += [f"- Verdict: **{review['verdict']}**; confidence {review['confidence_before']} -> "
            f"{review['confidence_after']} (adjustment {review['adjustment']})"
            + ("; the incident was escalated instead of acted on" if review["escalated"] else ""),
            f"- Tokens in/out: {review['tokens_in']}/{review['tokens_out']}; {review['seconds']}s"]
    out += [f"- Objection: {o['text']} ({', '.join(o['evidence'])})" for o in review["objections"]]
    if review.get("dropped_objections"):
        out.append(f"- {review['dropped_objections']} objection(s) dropped: no valid evidence IDs")
    alt = review.get("alternative_root_cause")
    if alt:
        out.append(f"- Suggested alternative (for the human only, never acted on): {alt['component']} "
                   f"[{alt.get('fault_class')}]: {alt.get('reason', '')}")
    return out


def build_report(record):
    record = upgrade(record)
    meta, d = record["metadata"], record.get("diagnosis") or {}
    plan = record.get("remediation_plan") or {"steps": []}
    out_, acc = record.get("outcome") or {}, record["accounting"]
    evidence = {e["id"]: e for e in record.get("evidence", [])}
    out = [f"# Incident report: run {meta['run_id']}", "",
           f"Incident {meta.get('incident_id')}. Mode: {meta['mode']} ({meta.get('source_detail', '')}). "
           f"Model: {meta.get('model')}. Record status: {record['status']}.", "",
           f"**Outcome: {out_.get('status', 'not finished')}.** {out_.get('summary', '')}", "",
           f"Diagnosis: **{d.get('status')}**, confidence {_fmt_conf(d.get('confidence'))}. "
           f"Plan: **{plan.get('outcome', 'not run')}**.", "", d.get("headline", ""), ""]

    out += ["## Timeline", "", "| When | What |", "|---|---|"]
    out += [f"| {when} | {what} |" for when, what in _timeline(record)]

    out += ["", "## Root cause", ""]
    for rc in d.get("root_causes") or []:
        state = " (resolved before action)" if rc.get("status") == "resolved" else ""
        out.append(f"- **{rc['component']}** [{rc['fault_class']}]{state}, confidence {_fmt_conf(rc['confidence'])}: "
                   f"{rc['cause']} (evidence {', '.join(rc['evidence'])})")
    if not d.get("root_causes"):
        out.append("- Not determined with valid evidence; escalated to a human.")
    if plan.get("resolved"):
        out += ["", "Resolved before action (no step created):"]
        out += [f"- {r['component']} [{r['fault_class']}]: {r.get('cause', '')} (evidence {', '.join(r.get('evidence', []))})"
                for r in plan["resolved"]]
    if d.get("affected_services"):
        out += ["", "Affected services:"] + [f"- {a['service']}: {a['impact']}" for a in d["affected_services"]]
    if d.get("ruled_out"):
        out += ["", "Ruled out:"] + [f"- {r['service']}: {r['reason']} ({', '.join(r['evidence'])})" for r in d["ruled_out"]]
    validation = d.get("validation") or {}
    if validation.get("overrides") or validation.get("dropped_evidence_ids"):
        out += ["", "Validator:"]
        out += [f"- override {o['field']}: {o['proposed']} -> {o['applied']} ({o['reason']})"
                for o in validation.get("overrides", [])]
        if validation.get("dropped_evidence_ids"):
            out.append(f"- dropped evidence IDs not in the registry: {', '.join(validation['dropped_evidence_ids'])}")

    out += _review_section(record.get("review"))

    out += ["", "## Evidence", "", "| ID | Tool | Observation |", "|---|---|---|"]
    for e in record.get("evidence", []):
        if e.get("cited"):
            out.append(f"| {e['id']} | {e.get('tool', '?')} | {e.get('observation', '').replace('|', '/')} |")

    out += ["", "## Actions taken", ""]
    if not any((st.get("execution") or {}).get("status") == "executed" for st in plan["steps"]):
        out.append("No action was executed on the system" + (" (dry run)." if plan.get("outcome") == "dry_run" else "."))
    if plan.get("client"):
        out.append(f"Action client: {plan['client']}.")
    for step in plan["steps"]:
        out += [""] + _step_section(step, evidence)
    if plan.get("stopped_reason"):
        out += ["", f"Plan stopped: {plan['stopped_reason']}."]

    for mv in record.get("manual_verifications") or []:
        out += ["", f"## Manual remediation (verified {mv['at']}): {mv['outcome']}", "",
                "Fixed by a human outside the agent; no Action API call was made. Verify-only result:"]
        for t in mv["targets"]:
            out += ["", f"### {t['target']} (checked {', '.join(t['involved'])})", ""]
            out += _verify_lines(t["verify"], t["target"], evidence)

    if record.get("escalations"):
        out += ["", "## Escalated to a human", ""]
        for e in record["escalations"]:
            out.append(f"- **{e.get('target')}** [{e.get('fault_class') or '?'}]: {e.get('reason', '')}")
            out += [f"  - {n}" for n in e.get("next_steps", [])]

    out += ["", "## What would prevent a repeat", ""]
    out += [f"- {p}" for p in out_.get("prevention") or d.get("prevention") or ["(none recorded)"]]
    if d.get("open_questions"):
        out += ["", "## Open questions", ""] + [f"- {q}" for q in d["open_questions"]]
    out += ["", "## Accounting", "",
            f"- Tokens in/out: {acc['tokens_in']}/{acc['tokens_out']}; estimated cost {_fmt_cost(acc.get('estimated_cost_usd'))}",
            f"- Model turns: {acc.get('steps_used')} of {acc.get('step_budget')}",
            f"- Time: investigation {acc['phases'].get('investigation_s')}s, remediation "
            f"{acc['phases'].get('remediation_s')}s (verify {acc['phases'].get('verify_s')}s), "
            f"total {acc['phases'].get('total_s')}s"]
    git = meta.get("git") or {}
    out.append(f"- Code: commit {git.get('commit')}{' (uncommitted changes)' if git.get('dirty') else ''}")
    return "\n".join(out) + "\n"


def to_organiser_schema(record):
    """STUB: map the incident record to the organisers' JSON schema once it is published.
    Until then it returns our own fields under neutral names, so the mapping lives in one place."""
    record = upgrade(record)
    d, plan = record.get("diagnosis") or {}, record.get("remediation_plan") or {"steps": []}
    cited = [e for e in record.get("evidence", []) if e.get("cited")]
    return {
        "schema": "UNCONFIRMED: organisers' schema not received yet",
        "incident_id": record["metadata"].get("incident_id"),
        "status": d.get("status"),
        "confidence": d.get("confidence"),
        "root_causes": [{k: rc.get(k) for k in ("component", "fault_class", "cause", "confidence", "evidence")}
                        for rc in d.get("root_causes") or []],
        "evidence": {e["id"]: e["observation"] for e in cited},
        "actions": [{"action": st["action"], "target": st["target"],
                     "result": (st.get("execution") or {}).get("status", "not run"),
                     "verify": (st.get("verify") or {}).get("outcome")} for st in plan["steps"]],
        "escalations": [{"target": e.get("target"), "reason": e.get("reason")} for e in record.get("escalations") or []],
        "manual_remediation": [{"verified_at": mv["at"], "outcome": mv["outcome"]}
                               for mv in record.get("manual_verifications") or []],
        "outcome": (record.get("outcome") or {}).get("status"),
        "prevention": (record.get("outcome") or {}).get("prevention") or d.get("prevention") or [],
    }


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("run", help="run record JSON (runs/<run_id>.json)")
    p.add_argument("--out", help="output markdown path")
    args = p.parse_args()
    path = Path(args.run)
    out = Path(args.out) if args.out else path.with_suffix(".report.md")
    out.write_text(build_report(load(path)), encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
