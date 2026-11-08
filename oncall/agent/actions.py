"""Remedies: the Action API's allowlist, which fault classes each action can fix, rollbacks, and
the ordered remediation plan.

The remedy is chosen from the diagnosed FAULT CLASS, never from the service name. Every root cause
with an allowlisted fix becomes one plan step, dependencies first (cache/db before the services
that use them). Root causes without an allowlisted fix are escalated with findings and next steps;
the agent never restarts something just to "try something".

Only the organisers' Action API may change the system (HARD RULE 1). This module decides and
checks; oncall/remediate.py runs the plan through oncall/action_api.py.
"""
from oncall import config

ESCALATE = "escalate_to_human"  # a decision for a human, not an Action API call

# Exactly the Action API's four actions (organisers' brief). inverse: how to roll it back
# (params are placeholders until the Action API spec is published).
ACTIONS = {
    "restart_service": {"available": True, "inverse": None},
    "toggle_feature_flag": {"available": True,
                            "inverse": {"action": "toggle_feature_flag", "params": {"restore": True},
                                        "note": "toggle the same flag back to its previous value"}},
    "throttle_load_generator": {"available": True,
                                "inverse": {"action": "throttle_load_generator", "params": {"remove": True},
                                            "note": "remove the throttle"}},
    "scale_service": {"available": False, "inverse": None,
                      "unavailable_because": "Kubernetes only; this demo runs on Docker Compose"},
}
AVAILABLE_ACTIONS = [a for a, spec in ACTIONS.items() if spec["available"]]

# Fault class -> the one allowlisted action that can fix it. Classes not listed have no
# allowlisted fix and are escalated.
FIXED_BY = {
    "container_stopped_or_restarting": "restart_service",
    "dependency_down": "restart_service",
    "traffic_flood": "throttle_load_generator",
    # Application faults that the demo's failure switches cause. Only behind ALLOW_FLAG_TOGGLE.
    "service_error": "toggle_feature_flag",
    "input_specific_error": "toggle_feature_flag",
    "memory_leak": "toggle_feature_flag",
    "cpu_or_gc_pressure": "toggle_feature_flag",
}

_APP_FAULT_STEPS = [
    "Read the failing operation's error message and the cited traces and logs for the cause.",
    "If a failure switch is suspected, a human can toggle it through the Action API "
    "(automatic flag toggling is disabled).",
]
_RESTART_MITIGATION = ("Option for a human: restart the service as a temporary mitigation. It frees "
                       "memory/CPU but does not fix the cause; the agent never does this automatically.")
NEXT_STEPS = {
    "network_degradation": [
        "Compare client and server span durations on the affected edge to locate the delay or loss.",
        "Check the Docker network between the two services (latency or loss rules, DNS).",
        "No allowlisted action fixes a network fault; a human must remove the cause.",
    ],
    "resource_starvation": [
        "Compare the container's CPU and memory limits with its normal usage (docker inspect).",
        "Look for a recent limit change or CPU throttling.",
        "Changing limits is outside the Action API; a human must restore them.",
    ],
    "service_unreachable": [
        "Check whether the target container is running and listening on its port.",
        "Check DNS and the network path between the caller and the target.",
    ],
    "unknown": [
        "Review the cited evidence and the open questions.",
        "Collect more telemetry: traces of the alerting service, logs, container state over time.",
    ],
    "service_error": _APP_FAULT_STEPS,
    "input_specific_error": _APP_FAULT_STEPS,
    "memory_leak": ["Confirm memory keeps rising toward the limit with get_metric."]
                   + _APP_FAULT_STEPS + [_RESTART_MITIGATION],
    "cpu_or_gc_pressure": ["Confirm CPU and GC time with get_metric."]
                          + _APP_FAULT_STEPS + [_RESTART_MITIGATION],
}

PREVENTION = {
    "container_stopped_or_restarting": [
        "Alert on container exits and a rising restart count, not only on user-facing errors.",
        "Record why the process exited (exit code, OOM) and fix the cause before it recurs.",
    ],
    "dependency_down": [
        "Health-check and alert on the cache or database directly.",
        "Give callers timeouts, retries with backoff and graceful degradation when it is down.",
    ],
    "traffic_flood": [
        "Rate-limit at the entry proxy and alert on sudden request-rate jumps.",
        "Load-test for the peak traffic the shop must survive.",
    ],
    "service_error": [
        "Add a regression test for the failing operation and alert on its error rate.",
        "Review failure switches and recent changes before they reach production.",
    ],
    "input_specific_error": [
        "Validate inputs and test with the full product catalogue.",
        "Alert on errors that cluster on one attribute value.",
    ],
    "memory_leak": [
        "Bound caches and alert when memory grows steadily toward the limit.",
        "Run soak tests that would expose the leak before release.",
    ],
    "cpu_or_gc_pressure": [
        "Profile the hot path and tune GC; alert on CPU near the limit and on GC time.",
    ],
    "network_degradation": [
        "Alert on per-edge latency (client vs server span time) and packet loss.",
        "Set timeouts and retries with backoff between services.",
    ],
    "resource_starvation": [
        "Size limits from observed usage with headroom; alert at 80% of a limit.",
        "Put limit changes under change control.",
    ],
    "service_unreachable": [
        "Add readiness checks and alert when a service stops answering its callers.",
    ],
    "unknown": [
        "Close the telemetry gaps that kept this diagnosis uncertain (logs, traces, metrics).",
        "Write a runbook entry once the cause is known.",
    ],
}


def rollback_for(action):
    """How to undo an executed action, or None if it cannot (or need not) be undone."""
    spec = ACTIONS.get(action)
    return spec["inverse"] if spec else None


# --- dependency map ---------------------------------------------------------------------------

def depends_on(service, target, _seen=None):
    """True if service calls target directly or through other services (config.DEPENDENCIES)."""
    seen = _seen or set()
    for callee in config.DEPENDENCIES.get(service, []):
        if callee == target:
            return True
        if callee not in seen:
            seen.add(callee)
            if depends_on(callee, target, seen):
                return True
    return False


def _height(service, _seen=()):
    """0 for services that call nothing; callers sit above their callees. Dependencies go first."""
    callees = [c for c in config.DEPENDENCIES.get(service, []) if c not in _seen]
    return 1 + max(_height(c, (*_seen, service)) for c in callees) if callees else 0


def callers_of(service):
    return sorted(c for c, callees in config.DEPENDENCIES.items() if service in callees)


def involved_services(step):
    """Services whose health shows whether a step worked: its target and the target's callers.
    Throttling the load generator is judged at the entry points it sends traffic to."""
    if step["action"] == "throttle_load_generator":
        entries = config.DEPENDENCIES.get("load-generator", [])
        return entries + [c for e in entries for c in config.DEPENDENCIES.get(e, [])]
    return [step["target"]] + [c for c in callers_of(step["target"]) if c != "load-generator"]


# --- the plan ----------------------------------------------------------------------------------

def remedy_rules():
    """Lines for the system prompt, generated from the tables above so they never drift."""
    lines = [f"- The Action API has exactly these actions: {', '.join(ACTIONS)}."]
    for name, spec in ACTIONS.items():
        if not spec["available"]:
            lines.append(f"- {name} is UNAVAILABLE ({spec['unavailable_because']}): never propose it.")
    by_action = {}
    for fault_class, action in FIXED_BY.items():
        by_action.setdefault(action, []).append(fault_class)
    for action, classes in by_action.items():
        if action == "toggle_feature_flag" and not config.ALLOW_FLAG_TOGGLE:
            lines.append(f"- {', '.join(classes)} -> {ESCALATE} (flag toggling is disabled), "
                         "with findings and next steps.")
            continue
        target = {"restart_service": " on the root-cause component",
                  "throttle_load_generator": " (target load-generator)",
                  "toggle_feature_flag": f" with flag set to one of: {', '.join(config.ALLOWED_FLAG_NAMES)}"}
        lines.append(f"- {', '.join(classes)} -> {action}{target[action]}.")
    rest = [c for c in NEXT_STEPS if c not in FIXED_BY]
    lines.append(f"- {', '.join(rest)} -> {ESCALATE} with findings and next steps. "
                 "Never restart something just to try it.")
    lines.append("- Several root causes: report each one. Every root cause with an allowlisted fix "
                 "becomes its own step, dependencies first; each step gets its own approval.")
    return "\n".join(lines)


def _escalation(component, fault_class, reason):
    return {"action": ESCALATE, "target": component or "unknown", "fault_class": fault_class,
            "reason": reason, "next_steps": NEXT_STEPS.get(fault_class, NEXT_STEPS["unknown"])}


def hold_for_human(diag, reason):
    """Keep the diagnosis but offer no fix: every planned step becomes an escalation (in place)."""
    plan = diag.get("remediation_plan") or {"steps": [], "escalations": []}
    held = [dict(_escalation(s.get("component", s["target"]), s.get("fault_class"),
                             f"{reason}; {s['action']} on {s['target']} was not offered for approval"),
                 source="fallback model") for s in plan["steps"]]
    plan["steps"], plan["escalations"] = [], held + list(plan.get("escalations", []))
    plan["hold_reason"] = reason
    diag["remediation_plan"] = plan
    diag["status"] = "escalate"
    if plan["escalations"]:
        diag["proposed_action"] = dict(plan["escalations"][0])
    diag.setdefault("validation", {}).setdefault("notes", []).append(reason)


def _fix_for(rc, proposed):
    """(action, why_not): the allowlisted action for one root cause, or None and the reason."""
    fault_class = rc["fault_class"]
    action = FIXED_BY.get(fault_class)
    if action is None:
        return None, f"no allowlisted action fixes {fault_class}"
    if action == "toggle_feature_flag":
        if not config.ALLOW_FLAG_TOGGLE:
            return None, "flag toggling is disabled until the rules are confirmed"
        if proposed.get("flag") not in config.ALLOWED_FLAG_NAMES:
            return None, f"flag {proposed.get('flag')!r} is not in the allowed flag names"
    if not ACTIONS[action]["available"]:
        return None, f"{action} is unavailable"
    return action, None


def _step_reason(rc, action, target):
    """Why this step: this root cause's own cause text (or its first cited evidence), never boilerplate."""
    why = (rc.get("cause") or "").strip() or next(iter((rc.get("evidence_detail") or {}).values()), "")
    return f"{rc['component']} [{rc['fault_class']}]: {why} -> {action} on {target}."


def build_plan(root_causes, proposed, confident, threshold, notes, overrides=None):
    """Ordered remediation plan: {"steps": [...], "escalations": [...], "resolved": [...]}.

    Only ACTIVE root causes get steps. A root cause whose evidence shows it already recovered
    (status "resolved") is listed under "resolved" and never acted on.

    root_causes: validated, each with component / fault_class / confidence / cause.
    proposed:    the model's proposed_action; its wording is kept where it agrees with the plan.
    confident:   False (overall confidence below threshold) escalates everything.
    threshold:   a single root cause below it is escalated instead of acted on.
    notes:       validator notes; overrides of the model's proposal are recorded here.
    overrides:   list that receives each override as {field, proposed, applied, reason}.
    """
    overrides = [] if overrides is None else overrides
    proposed = dict(proposed or {})
    ranked = sorted(root_causes or [], key=lambda rc: -rc["confidence"])
    if not ranked or not confident:
        primary = ranked[0] if ranked else {}
        return {"steps": [], "escalations": [_escalation(
            primary.get("component"), primary.get("fault_class", "unknown"),
            "Confidence below threshold or no root cause with valid evidence; a human must decide.")]}

    steps, escalations, resolved = [], [], []
    for rc in ranked:
        component, fault_class = rc["component"], rc["fault_class"]
        if rc.get("status") == "resolved":
            resolved.append({"component": component, "fault_class": fault_class, "cause": rc.get("cause", ""),
                             "evidence": rc.get("evidence", []), "note": "resolved before action"})
            continue
        if rc["confidence"] < threshold:
            escalations.append(_escalation(component, fault_class,
                                           f"{component} [{fault_class}]: confidence {rc['confidence']} "
                                           f"below {threshold}; a human must decide."))
            continue
        action, why = _fix_for(rc, proposed)
        if action is None:
            escalations.append(_escalation(component, fault_class,
                                           f"{component} [{fault_class}]: {why}; escalating with findings."))
            continue
        target = "load-generator" if action == "throttle_load_generator" else component
        if any(s["action"] == action and s["target"] == target for s in steps):
            continue
        step = {"action": action, "target": target, "component": component, "fault_class": fault_class,
                "params": {}, "reason": _step_reason(rc, action, target),
                "rollback": rollback_for(action), "dry_run": True}
        if action == "toggle_feature_flag":
            step["flag"] = proposed["flag"]
            step["params"] = {"flag": proposed["flag"]}
        steps.append(step)

    # Dependencies first; among unrelated targets, the one more services rely on first (e.g. the
    # cache before payment); remaining ties keep the confidence order (sorted() is stable).
    dependents = {s["target"]: sum(depends_on(svc, s["target"]) for svc in config.DEPENDENCIES)
                  for s in steps}
    steps.sort(key=lambda s: (_height(s["target"]), -dependents[s["target"]]))
    same_action = [s for s in steps if s["action"] == proposed.get("action")]
    # The model's wording belongs to the step it proposed: the same target if there is one, else the
    # step its proposal was retargeted to (first of that action). Every other step keeps the reason
    # built from its own root cause, so no step gets boilerplate or another step's text.
    worded = next((s for s in same_action if proposed.get("target") in (s["target"], s.get("component"))),
                  same_action[0] if same_action else None)
    if worded:
        for key in ("reason", "risk_assessment", "expected_outcome"):
            if proposed.get(key):
                worded[key] = proposed[key]
    for i, s in enumerate(steps, 1):
        s["step"] = i

    if not steps and not escalations:  # every root cause already resolved: nothing to do
        if proposed.get("action") and proposed["action"] != ESCALATE:
            notes.append(f"proposed action {proposed['action']} dropped: all root causes resolved before action")
            overrides.append({"field": "proposed_action.action",
                              "proposed": {"action": proposed["action"], "target": proposed.get("target")},
                              "applied": {"action": "none", "target": None},
                              "reason": "all root causes were resolved before any action"})
        return {"steps": [], "escalations": [], "resolved": resolved}

    first = (steps or escalations)[0]
    if proposed.get("action") and proposed["action"] not in {s["action"] for s in steps} \
            and not (proposed["action"] == ESCALATE and not steps):
        reason = f"fault class {first['fault_class']} maps to {first['action']}"
        notes.append(f"proposed action {proposed['action']} replaced by {first['action']} "
                     f"(fault class {first['fault_class']})")
        overrides.append({"field": "proposed_action.action",
                          "proposed": {"action": proposed["action"], "target": proposed.get("target")},
                          "applied": {"action": first["action"], "target": first["target"]}, "reason": reason})
    elif worded and proposed.get("target") and proposed["target"] != worded["target"]:
        notes.append(f"proposed target {proposed['target']} replaced by {worded['target']}")
        overrides.append({"field": "proposed_action.target",
                          "proposed": {"action": proposed["action"], "target": proposed["target"]},
                          "applied": {"action": worded["action"], "target": worded["target"]},
                          "reason": "the action targets the root-cause component, not a victim"})
    return {"steps": steps, "escalations": escalations, "resolved": resolved}


def prevention_for(root_causes):
    """Default prevention recommendations for the fault classes found (deduplicated)."""
    out = []
    for rc in root_causes or [{"fault_class": "unknown"}]:
        for item in PREVENTION.get(rc.get("fault_class"), PREVENTION["unknown"]):
            if item not in out:
                out.append(item)
    return out


def execution_allowed(step):
    """(allowed, reason). Only an available Action API action with a human's explicit yes may run.

    step: a plan step (action + approval), or a diagnosis (proposed_action + approval).
    Auto-approval (--auto-approve) is for dry runs only and never allows execution.
    """
    action = step.get("action") or (step.get("proposed_action") or {}).get("action")
    approval = step.get("approval") or {}
    if action not in ACTIONS:
        return False, f"{action} is not an Action API action"
    if not ACTIONS[action]["available"]:
        return False, f"{action} is unavailable: {ACTIONS[action]['unavailable_because']}"
    if approval.get("status") != "approved":
        return False, f"not approved (status {approval.get('status')})"
    if approval.get("approved_by") != "human":
        return False, "auto-approval is dry-run only"
    if config.KILL_SWITCH_FILE.exists():
        return False, "kill switch is on"
    return True, "approved by a human"
