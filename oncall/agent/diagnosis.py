"""The submit_diagnosis tool, root-cause validation, and human approval gate.

Checkpoint 2 output is a full DIAGNOSIS in JSON schema:
  - affected services & anomalies with numbers vs baseline
  - root causes with component, fault_class, confidence, and cited evidence IDs
  - proposed safe remediation from allowlisted runbook actions
  - human approval gate (dry-run default for Day 1)
  - graceful escalation when confidence is low or evidence is insufficient
"""
from datetime import datetime, timezone
from oncall.evidence import registry

CONFIDENT = 0.6

FAULT_CLASSES = [
    "container_stopped_or_restarting",
    "dependency_down",
    "resource_starvation",
    "memory_leak",
    "cpu_or_gc_pressure",
    "service_error",
    "service_unreachable",
    "input_specific_error",
    "traffic_flood",
    "network_degradation",
    "unknown",
]

ALLOWLISTED_ACTIONS = [
    "restart_service",
    "toggle_feature_flag",
    "throttle_load_generator",
    "scale_service",
    "escalate_to_human",
    "none",
]

_EVIDENCE = {
    "type": "array",
    "items": {"type": "string"},
    "description": "Evidence IDs from tool results, e.g. [\"E3\", \"E7\"].",
}

SUBMIT_TOOL = {
    "name": "submit_diagnosis",
    "description": "Submit the final incident diagnosis, root cause, and proposed remediation plan. Call exactly once at the end.",
    "strict": False,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["headline", "affected_services", "anomalies", "root_causes",
                     "proposed_action", "open_questions"],
        "properties": {
            "headline": {"type": "string", "description": "One sentence summary for the on-call human."},
            "affected_services": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["service", "impact", "evidence"],
                    "properties": {
                        "service": {"type": "string"},
                        "impact": {"type": "string", "description": "What users or callers see."},
                        "evidence": _EVIDENCE,
                    },
                },
            },
            "anomalies": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["service", "signal", "observed", "baseline", "evidence"],
                    "properties": {
                        "service": {"type": "string"},
                        "signal": {"type": "string", "description": "e.g. error_rate, p95, container_state, log_errors, silent, rps"},
                        "observed": {"type": "string", "description": "The number or state now, copied from the evidence."},
                        "baseline": {"type": "string", "description": "The healthy number, or 'unknown'."},
                        "evidence": _EVIDENCE,
                    },
                },
            },
            "root_causes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["component", "fault_class", "cause", "confidence", "evidence"],
                    "properties": {
                        "component": {"type": "string", "description": "Failing service or container name."},
                        "fault_class": {"type": "string", "enum": FAULT_CLASSES, "description": "Standard fault classification."},
                        "cause": {"type": "string", "description": "Specific explanation of what broke and why."},
                        "confidence": {"type": "number", "description": "Confidence score 0.0 to 1.0 based strictly on evidence."},
                        "evidence": _EVIDENCE,
                    },
                },
            },
            "suspected_origin": {
                "type": "object",
                "additionalProperties": False,
                "required": ["service", "reasoning", "confidence", "evidence"],
                "properties": {
                    "service": {"type": "string", "description": "Where the problem starts, or 'unknown'."},
                    "reasoning": {"type": "string"},
                    "confidence": {"type": "number", "description": "0 to 1."},
                    "evidence": _EVIDENCE,
                },
            },
            "proposed_action": {
                "type": "object",
                "additionalProperties": False,
                "required": ["action", "target", "reason"],
                "properties": {
                    "action": {"type": "string", "enum": ALLOWLISTED_ACTIONS, "description": "Allowlisted runbook remedy."},
                    "target": {"type": "string", "description": "Service or container to act on."},
                    "reason": {"type": "string", "description": "Why this action fixes the incident."},
                    "dry_run": {"type": "boolean", "description": "True for read-only dry-run simulation (default True)."},
                    "risk_assessment": {"type": "string", "description": "Potential impact or blast radius."},
                    "expected_outcome": {"type": "string", "description": "Expected metric recovery."},
                },
            },
            "open_questions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "What is still unexplained or should be checked next.",
            },
        },
    },
}


def _clamp(x):
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return 0.0


def validate(diag):
    """Drop claims without valid evidence; synthesizes root_causes and suspected_origin,

    validates proposed remediation, and calculates status (diagnosed vs escalate).
    """
    diag = dict(diag)
    notes, dropped_ids = [], []

    def keep_valid(items, label):
        kept = []
        for item in items or []:
            valid, dropped = registry.validate(item.get("evidence"))
            dropped_ids.extend(dropped)
            if not valid:
                notes.append(f"dropped {label} without valid evidence: {item.get('service') or item.get('component')}")
                continue
            kept.append({**item, "evidence": valid})
        return kept

    diag["affected_services"] = keep_valid(diag.get("affected_services"), "affected service")
    diag["anomalies"] = keep_valid(diag.get("anomalies"), "anomaly")

    # Validate root_causes
    raw_rcs = diag.get("root_causes") or []
    valid_rcs = []
    for rc in raw_rcs:
        valid_ev, dropped = registry.validate(rc.get("evidence"))
        dropped_ids.extend(dropped)
        if not valid_ev:
            notes.append(f"dropped root cause without valid evidence: {rc.get('component')}")
            continue
        rc_clean = dict(rc)
        rc_clean["evidence"] = valid_ev
        rc_clean["confidence"] = round(_clamp(rc_clean.get("confidence", 0.0)), 2)
        rc_clean["evidence_detail"] = {e: registry.get(e)["summary"] for e in valid_ev if registry.get(e)}
        if rc_clean.get("fault_class") not in FAULT_CLASSES:
            rc_clean["fault_class"] = "unknown"
        valid_rcs.append(rc_clean)

    # Validate or synthesize suspected_origin
    origin = dict(diag.get("suspected_origin") or {})
    origin_ev, dropped = registry.validate(origin.get("evidence"))
    dropped_ids.extend(dropped)

    if origin_ev:
        origin["evidence"] = origin_ev
        origin["confidence"] = round(_clamp(origin.get("confidence", 0.0)), 2)
        origin["evidence_detail"] = {e: registry.get(e)["summary"] for e in origin_ev if registry.get(e)}
    elif origin.get("service") not in (None, "", "unknown"):
        notes.append(f"dropped suspected origin without valid evidence: {origin.get('service')}")
        origin = {}

    # Interoperability: synthesize root_causes from suspected_origin if needed
    if not valid_rcs and origin and origin.get("service") not in ("", "unknown"):
        valid_rcs.append({
            "component": origin["service"],
            "fault_class": "unknown",
            "cause": origin.get("reasoning", ""),
            "confidence": origin.get("confidence", 0.0),
            "evidence": origin.get("evidence", []),
            "evidence_detail": origin.get("evidence_detail", {}),
        })

    # Interoperability: synthesize suspected_origin from root_causes if needed
    if not origin and valid_rcs:
        primary = valid_rcs[0]
        origin = {
            "service": primary["component"],
            "reasoning": primary["cause"],
            "confidence": primary["confidence"],
            "evidence": primary["evidence"],
            "evidence_detail": primary.get("evidence_detail", {}),
        }
    elif not origin:
        origin = {"service": "unknown", "reasoning": "no valid evidence", "confidence": 0.0, "evidence": []}

    diag["root_causes"] = valid_rcs
    diag["suspected_origin"] = origin

    # Overall confidence and status
    confidence = max([rc["confidence"] for rc in valid_rcs], default=origin.get("confidence", 0.0))
    diag["confidence"] = round(confidence, 2)

    # diagnosed: a root cause backed by valid evidence with enough confidence; otherwise escalate.
    if valid_rcs and confidence >= CONFIDENT:
        diag["status"] = "diagnosed"
    else:
        diag["status"] = "escalate"
        notes.append("no root cause with valid evidence; escalated to human" if not valid_rcs
                     else f"confidence {confidence:.2f} below {CONFIDENT}; escalated to human")

    # Validate proposed remediation
    action = dict(diag.get("proposed_action") or {})
    if diag["status"] == "escalate":
        action = {
            "action": "escalate_to_human",
            "target": origin.get("service", "unknown"),
            "reason": "Confidence below threshold or ambiguous evidence; escalate to on-call engineer.",
        }
    elif not action or action.get("action") not in ALLOWLISTED_ACTIONS:
        if valid_rcs:
            action = {"action": "restart_service", "target": valid_rcs[0]["component"],
                      "reason": f"Restart {valid_rcs[0]['component']} to recover from {valid_rcs[0]['fault_class']}."}
        else:
            action = {"action": "none", "target": "", "reason": "No actionable root cause identified."}
    action["dry_run"] = True  # Strict Day 1 rule: dry-run only
    diag["proposed_action"] = action

    # Default approval state
    diag["approval"] = diag.get("approval", {
        "status": "pending",
        "requested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dry_run": True,
        "executed": False,
    })

    diag["validation"] = {"dropped_evidence_ids": sorted(set(dropped_ids)), "notes": notes}
    return diag


def request_human_approval(diag, auto_approve=False, interactive=True):
    """Approval gate: present diagnosis and proposed action to human for explicit sign-off."""
    import sys
    approval = diag.get("approval", {})
    action = diag.get("proposed_action", {})
    primary = (diag.get("root_causes") or [{}])[0]

    if auto_approve:
        approval["status"] = "approved"
        approval["decision_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        approval["notes"] = "Auto-approved via flag"
        diag["approval"] = approval
        return diag

    if not interactive or not hasattr(sys.stdin, "isatty") or not sys.stdin.isatty():
        approval["status"] = "pending"
        diag["approval"] = approval
        return diag

    print("\n" + "=" * 64)
    print("   INCIDENT DIAGNOSIS & REMEDIATION APPROVAL GATE")
    print("=" * 64)
    print(f"Status           : {diag.get('status', 'unknown').upper()}")
    print(f"Primary Component: {primary.get('component', diag.get('suspected_origin', {}).get('service'))}")
    print(f"Fault Class      : {primary.get('fault_class', 'unknown')}")
    print(f"Confidence       : {diag.get('confidence', 0.0):.2f}")
    print(f"Root Cause       : {primary.get('cause', diag.get('headline'))}")
    print(f"Proposed Action  : {action.get('action')} on {action.get('target')}")
    print(f"Reason           : {action.get('reason')}")
    print("-" * 64)
    print("[NOTE] Day 1 Policy: Read-only simulation mode (dry_run=True).")

    try:
        choice = input("Approve this remediation plan? [y/N]: ").strip().lower()
        if choice in ("y", "yes"):
            approval["status"] = "approved"
            approval["notes"] = "Explicitly approved by on-call engineer"
        else:
            approval["status"] = "rejected"
            approval["notes"] = "Rejected by on-call engineer"
    except (EOFError, KeyboardInterrupt):
        approval["status"] = "rejected"
        approval["notes"] = "Cancelled or interrupted"

    approval["decision_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    diag["approval"] = approval
    return diag


def empty_summary(reason):
    return validate({
        "headline": f"Investigation ended without a diagnosis ({reason}).",
        "affected_services": [],
        "anomalies": [],
        "root_causes": [],
        "suspected_origin": {"service": "unknown", "reasoning": reason, "confidence": 0.0, "evidence": []},
        "proposed_action": {"action": "escalate_to_human", "target": "unknown", "reason": reason},
        "open_questions": [],
    })


def render(diag):
    rcs = diag.get("root_causes") or []
    act = diag.get("proposed_action") or {}
    app = diag.get("approval") or {}
    source = diag.get("source", "?")
    lines = [f"\n--- DIAGNOSIS [{diag['status']}] source={source} ({diag.get('source_detail', '')})"]
    if source == "test":
        lines.append("!!! TEST MODE: fixture data, not the live demo")
    lines += [diag.get("headline", ""), "\nAffected Services:"]
    lines += [f"  - {a['service']}: {a['impact']} [{', '.join(a['evidence'])}]"
              for a in diag.get("affected_services", [])]
    lines.append("\nObserved Anomalies:")
    lines += [f"  - {a['service']} {a['signal']}: {a['observed']} (baseline {a['baseline']}) [{', '.join(a['evidence'])}]"
              for a in diag.get("anomalies", [])]
    if rcs:
        lines.append(f"\nRoot Cause(s) [Overall Confidence: {diag.get('confidence', 0.0):.2f}]:")
        for rc in rcs:
            lines.append(f"  * {rc['component']} [{rc['fault_class']}] (confidence {rc['confidence']:.2f}): {rc['cause']}")
            for e, text in rc.get("evidence_detail", {}).items():
                lines.append(f"      {e}: {text[:140]}")
    else:
        o = diag.get("suspected_origin", {})
        lines.append(f"\nSuspected origin: {o.get('service')} (confidence {o.get('confidence', 0.0)}) - {o.get('reasoning', '')}")
    lines.append(f"\nProposed Remediation: {act.get('action')}(target={act.get('target')}) [dry_run={act.get('dry_run', True)}]")
    lines.append(f"  Reason: {act.get('reason')}")
    lines.append(f"\nApproval Gate: {app.get('status', 'pending').upper()}")
    if diag.get("open_questions"):
        lines.append("\nOpen Questions / Next Checks:")
        lines += [f"  - {q}" for q in diag["open_questions"]]
    if diag.get("validation", {}).get("notes"):
        lines += [f"(validator) {n}" for n in diag["validation"]["notes"]]
    return "\n".join(lines)
