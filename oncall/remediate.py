"""Run a remediation plan one step at a time: approve -> kill switch -> act -> verify.

For every step, in plan order (dependencies first):
  1. kill switch check, then ask a human for THIS step (auto-approval is dry-run only)
  2. execution_allowed() for a live client; kill switch checked again right before the call
  3. the call goes through an Action API client (oncall/action_api.py), never anything else
  4. verify against the baseline (oncall/verify.py) before the next step is offered
  5. not recovered: roll back the step if it has an inverse (kill switch checked first), stop,
     escalate what is left. Partial recovery continues only when the services still unhealthy
     depend on a later step's target; otherwise it stops (the step is kept, it helped).

Every call, refusal, failure, verify result and rollback is appended to runs/actions.jsonl.
"""
import sys
from datetime import datetime, timezone

from oncall import config
from oncall.action_api import ActionApiError
from oncall.agent.actions import depends_on, execution_allowed, involved_services
from oncall.record import append_line
from oncall.verify import INCONCLUSIVE, NOT_RECOVERED, PARTIAL, RECOVERED


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_action(event, run_id, step, **fields):
    """Append one event to the action log (JSON lines)."""
    entry = {"ts": _now(), "run_id": run_id, "event": event, "step": step.get("step"),
             "action": step.get("action"), "target": step.get("target")}
    if step.get("flag"):
        entry["flag"] = step["flag"]
    entry.update(fields)
    append_line(config.ACTION_LOG, entry)  # scrubbed: no secrets, no feature-flag data
    return entry


# --- approval ---------------------------------------------------------------------------------

def risk_of(step):
    """The step's risk assessment for the approval box and record.
    (Based on Shivakumar's Day 1 approval gate, which showed the risk to the approver.)"""
    return (step.get("risk_assessment") or "").strip() or "not assessed"


def make_approver(auto_approve=False, interactive=True):
    """approve(step, diag) -> approval record for that one step."""

    def approve(step, diag):
        record = approval_for(step, diag)
        return {**record, "risk": risk_of(step)}  # the risk the decision was made on

    def approval_for(step, diag):
        if auto_approve:
            return {"status": "approved", "approved_by": "auto", "decision_at": _now(),
                    "notes": "auto-approved via flag: dry run only, never executed"}
        if not interactive or not hasattr(sys.stdin, "isatty") or not sys.stdin.isatty():
            return {"status": "pending", "decision_at": _now(), "notes": "no terminal: nobody to approve"}
        steps = (diag.get("remediation_plan") or {}).get("steps", [])
        print("\n" + "=" * 64)
        print(f"   APPROVAL NEEDED: step {step['step']} of {len(steps)}")
        print("=" * 64)
        print(f"Diagnosis  : {diag.get('status', '?').upper()} (confidence {diag.get('confidence', 0.0):.2f})")
        print(f"Root cause : {step.get('component')} [{step.get('fault_class')}]")
        print(f"Action     : {step['action']} on {step['target']}" + (f" (flag {step['flag']})" if step.get("flag") else ""))
        print(f"Reason     : {step.get('reason')}")
        if step.get("expected_outcome"):
            print(f"Expected   : {step['expected_outcome']}")
        print(f"Risk       : {risk_of(step)}")
        rb = step.get("rollback")
        print(f"Rollback   : {rb['action']} ({rb['note']})" if rb else "Rollback   : none (cannot be undone)")
        print("-" * 64)
        try:
            choice = input(f"Run step {step['step']} now? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return {"status": "rejected", "decision_at": _now(), "notes": "cancelled or interrupted"}
        if choice in ("y", "yes"):
            return {"status": "approved", "approved_by": "human", "decision_at": _now(),
                    "notes": "explicitly approved by the on-call engineer"}
        return {"status": "rejected", "decision_at": _now(), "notes": "rejected by the on-call engineer"}

    return approve


def _may_run(step, client):
    if client.live:
        return execution_allowed(step)
    if (step.get("approval") or {}).get("status") == "approved":
        return True, "dry run (nothing is sent)"
    return False, f"not approved (status {(step.get('approval') or {}).get('status')})"


# --- the executor ----------------------------------------------------------------------------

def _explained_by_later_steps(unhealthy, rest):
    """True when every still-unhealthy service depends on (or is) a later step's target."""
    return bool(unhealthy) and all(
        any(svc == s["target"] or depends_on(svc, s["target"]) for s in rest) for svc in unhealthy)


def _rollback(step, client, run_id, log):
    inverse = step.get("rollback")
    if not inverse:
        step["rollback_result"] = {"status": "none", "reason": f"{step['action']} has no inverse"}
        return
    if config.KILL_SWITCH_FILE.exists():
        step["rollback_result"] = {"status": "refused", "reason": "kill switch is on"}
        log_action("rollback_refused", run_id, step, reason="kill switch is on")
        log(f"[plan] step {step['step']}: rollback REFUSED (kill switch is on)")
        return
    try:
        res = client.call(inverse["action"], step["target"], step.get("flag"), inverse.get("params"),
                          idempotency_key=f"{run_id}:step{step['step']}:rollback")
    except ActionApiError as e:
        step["rollback_result"] = {"status": "failed", "error": str(e)}
        log_action("rollback_failed", run_id, step, error=str(e))
        log(f"[plan] step {step['step']}: rollback FAILED: {e}")
        return
    step["rollback_result"] = {"status": "done" if client.live else "dry_run", "at": _now(),
                               "request": res["request"], "response": res.get("response")}
    log_action("rollback", run_id, step, request=res["request"], response=res.get("response"),
               note=inverse["note"])
    log(f"[plan] step {step['step']}: rolled back ({inverse['note']})")


def _stop(plan, rest, why):
    plan["stopped_reason"] = why
    plan["not_run"] = [{"step": s["step"], "action": s["action"], "target": s["target"],
                        "component": s.get("component"), "fault_class": s.get("fault_class"),
                        "why": why} for s in rest]


def verify_manual(diag, verifier, run_id="", log=print):
    """Verify-only mode: a human fixed the system by hand, outside the agent. Nothing is called;
    each root-cause component is verified like a step and logged as "manual_remediation"."""
    components = []
    for rc in diag.get("root_causes") or []:
        if rc["component"] not in components:
            components.append(rc["component"])
    for step in (diag.get("remediation_plan") or {}).get("steps", []):
        if step.get("component", step["target"]) not in components:
            components.append(step.get("component", step["target"]))
    results = []
    for i, component in enumerate(components, 1):
        step = {"step": i, "action": "manual_remediation", "target": component}
        involved = involved_services(step)
        log(f"[verify] manual fix of {component}: checking {', '.join(involved)} against the baseline...")
        v = verifier.verify(step, involved, progress=log)
        log_action("manual_remediation", run_id, step, outcome=v["outcome"], unhealthy=v["unhealthy"],
                   evidence=v["evidence"], note="fixed by a human outside the agent; no Action API call")
        log(f"[verify] {component}: {v['outcome'].upper()}")
        results.append({"target": component, "involved": involved, "verify": v})
    outcomes = [r["verify"]["outcome"] for r in results]
    if not outcomes or all(o == INCONCLUSIVE for o in outcomes):
        overall = INCONCLUSIVE
    elif all(o == RECOVERED for o in outcomes):
        overall = RECOVERED
    elif all(o == NOT_RECOVERED for o in outcomes):
        overall = NOT_RECOVERED
    else:
        overall = PARTIAL
    return {"at": _now(), "outcome": overall, "targets": results}


def run_plan(diag, client, approve, verifier, run_id="", log=print, on_update=None):
    """Walk diag["remediation_plan"]["steps"]; returns the plan with per-step results and an outcome.
    on_update() is called after every event (approval, action, verify, rollback) so the caller can
    save the incident record incrementally."""
    update = on_update or (lambda: None)
    plan = diag.setdefault("remediation_plan", {"steps": [], "escalations": []})
    plan.update(client=client.name, live=client.live, not_run=[], stopped_reason=None)
    steps = plan["steps"]
    if not steps:
        plan["outcome"], plan["escalate"] = "escalated", True
        log("[plan] no allowlisted action: escalating to a human")
        update()
        return plan

    def switch():
        return "on" if config.KILL_SWITCH_FILE.exists() else "off"

    def stop(step, rest, outcome, why):
        step["outcome"] = outcome
        for s in rest:
            s["outcome"] = "not_run"
        _stop(plan, rest, why)
        update()

    stopped = False
    for i, step in enumerate(steps):
        rest = steps[i + 1:]
        label = f"[plan] step {step['step']}/{len(steps)} {step['action']} {step['target']}"
        step["kill_switch"] = {"before_approval": switch()}
        if step["kill_switch"]["before_approval"] == "on":
            step["execution"] = {"status": "refused", "reason": "kill switch is on", "at": _now()}
            log_action("refused", run_id, step, reason="kill switch is on")
            log(f"{label}: REFUSED (kill switch is on)")
            stop(step, rest, "refused", "kill switch is on")
            stopped = True
            break

        step["approval"] = approve(step, diag)
        update()
        allowed, reason = _may_run(step, client)
        if not allowed:
            step["execution"] = {"status": "refused", "reason": reason, "at": _now()}
            log_action("refused", run_id, step, reason=reason, approval=step["approval"])
            log(f"{label}: NOT RUN ({reason})")
            stop(step, rest, "refused", f"step {step['step']} was not run: {reason}")
            stopped = True
            break

        involved = involved_services(step)
        before = None
        if client.live:
            try:
                before = verifier.measure(involved)
            except Exception as e:  # a missing "before" must not block an approved fix
                before = {"error": str(e)[:200]}

        step["kill_switch"]["before_action"] = switch()  # again, right before the action
        if step["kill_switch"]["before_action"] == "on":
            step["execution"] = {"status": "refused", "reason": "kill switch is on", "at": _now()}
            log_action("refused", run_id, step, reason="kill switch is on")
            log(f"{label}: REFUSED (kill switch is on)")
            stop(step, rest, "refused", "kill switch is on")
            stopped = True
            break
        try:
            # one key per run and step: a retried request is never applied twice by the Action API
            res = client.call(step["action"], step["target"], step.get("flag"), step.get("params") or None,
                              idempotency_key=f"{run_id}:step{step['step']}:{step['action']}")
        except ActionApiError as e:
            step["execution"] = {"status": "failed", "error": str(e), "at": _now()}
            log_action("failed", run_id, step, error=str(e), approval=step["approval"])
            log(f"{label}: FAILED ({e})")
            stop(step, rest, "failed", f"step {step['step']} failed: {e}")
            stopped = True
            break
        step["dry_run"] = not client.live
        step["execution"] = {"status": "executed" if client.live else "dry_run", "at": _now(),
                             "request": res["request"], "http_status": res.get("status"),
                             "response": res.get("response")}
        log_action("call" if client.live else "dry_run", run_id, step, request=res["request"],
                   response=res.get("response"), approval=step["approval"])
        log(f"{label}: {'EXECUTED' if client.live else 'dry run'} -> {res['request']['method']} "
            f"{res['request']['url']}")
        update()

        if not client.live:
            step["verify"] = {"outcome": "skipped", "reason": "dry run: nothing changed"}
            step["outcome"] = "dry_run"
            update()
            continue
        log(f"{label}: verifying {', '.join(involved)} against the baseline...")
        v = verifier.verify(step, involved, before, progress=log)
        step["verify"] = v
        step["outcome"] = v["outcome"]
        log_action("verify", run_id, step, outcome=v["outcome"], unhealthy=v["unhealthy"],
                   evidence=v["evidence"])
        log(f"{label}: verify {v['outcome'].upper()}" + (f" (still unhealthy: {', '.join(v['unhealthy'])})"
                                                          if v["unhealthy"] else ""))
        update()
        if v["outcome"] == RECOVERED:
            continue
        if v["outcome"] == PARTIAL and _explained_by_later_steps(v["unhealthy"], rest):
            v["note"] = "partial: the services still unhealthy depend on a later step"
            continue
        if v["outcome"] == NOT_RECOVERED:
            step["kill_switch"]["before_rollback"] = switch()
            _rollback(step, client, run_id, log)
        stop(step, rest, v["outcome"], f"step {step['step']} verify: {v['outcome']}")
        stopped = True
        break

    if stopped:
        plan["outcome"] = "stopped"
    elif not client.live:
        plan["outcome"] = "dry_run"
    else:
        last = steps[-1]["verify"]["outcome"]
        plan["outcome"] = "recovered" if last == RECOVERED else "partial"
    plan["escalate"] = bool(plan["escalations"] or plan["not_run"]) or plan["outcome"] not in (
        "recovered", "dry_run")
    update()
    return plan
