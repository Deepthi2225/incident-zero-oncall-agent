"""The submit_diagnosis tool, root-cause validation and the remediation plan.

Checkpoint 2 output is a full DIAGNOSIS in JSON schema:
  - affected services & anomalies with numbers vs baseline
  - root causes with component, fault_class, confidence, and cited evidence IDs
  - proposed safe remediation from allowlisted runbook actions
  - an ordered remediation plan; each step is approved and run by oncall/remediate.py
  - graceful escalation when confidence is low or evidence is insufficient
"""
from oncall.agent.actions import AVAILABLE_ACTIONS, ESCALATE, build_plan, prevention_for
from oncall.evidence import registry

CONFIDENT = 0.6
ROOT_CAUSE_STATUS = ("active", "resolved")

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

# What the model may propose: the available Action API actions, or escalation to a human.
# The validator then enforces the fault-class mapping in oncall/agent/actions.py.
PROPOSABLE_ACTIONS = AVAILABLE_ACTIONS + [ESCALATE]

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
                     "proposed_action", "prevention", "open_questions"],
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
                    "required": ["component", "fault_class", "cause", "confidence", "status", "evidence"],
                    "properties": {
                        "component": {"type": "string", "description": "Failing service or container name."},
                        "status": {"type": "string", "enum": ["active", "resolved"],
                                   "description": "active: still failing now. resolved: the evidence shows it "
                                                  "already recovered (e.g. running and serving again); no "
                                                  "action is taken for it."},
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
                    "action": {"type": "string", "enum": PROPOSABLE_ACTIONS,
                               "description": "Allowlisted remedy for the root cause's fault class, or escalate_to_human."},
                    "target": {"type": "string", "description": "Service or container to act on."},
                    "flag": {"type": "string", "description": "Flag name, only for toggle_feature_flag."},
                    "reason": {"type": "string", "description": "Why this action fixes the incident."},
                    "dry_run": {"type": "boolean", "description": "True for read-only dry-run simulation (default True)."},
                    "risk_assessment": {"type": "string", "description": "Potential impact or blast radius."},
                    "expected_outcome": {"type": "string", "description": "Expected metric recovery."},
                },
            },
            "ruled_out": {
                "type": "array",
                "description": "Suspects you checked and cleared, each with the evidence that clears it.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["service", "reason", "evidence"],
                    "properties": {
                        "service": {"type": "string"},
                        "reason": {"type": "string", "description": "Why it is not the cause."},
                        "evidence": _EVIDENCE,
                    },
                },
            },
            "prevention": {
                "type": "array",
                "items": {"type": "string"},
                "description": "What would prevent a repeat, specific to this root cause.",
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
    diag["ruled_out"] = keep_valid(diag.get("ruled_out"), "ruled-out suspect")

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
        if rc_clean.get("status") not in ROOT_CAUSE_STATUS:
            if rc_clean.get("status") is not None:
                notes.append(f"root cause {rc_clean.get('component')}: status {rc_clean['status']!r} "
                             f"is not active/resolved; treated as active")
            rc_clean["status"] = "active"
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

    # Remedy: an ordered plan decided by fault class (oncall/agent/actions.py), never by service
    # name. proposed_action is the first step (or the first escalation) for a one-line summary.
    overrides = []
    plan = build_plan(valid_rcs, diag.get("proposed_action"), diag["status"] == "diagnosed",
                      CONFIDENT, notes, overrides)
    diag["remediation_plan"] = plan
    first = (plan["steps"] or plan["escalations"] or [None])[0]
    diag["proposed_action"] = dict(first) if first else {
        "action": "none", "target": None, "reason": "all root causes were resolved before any action"}

    prevention = [p for p in diag.get("prevention") or [] if isinstance(p, str) and p.strip()]
    diag["prevention"] = prevention or prevention_for(valid_rcs)

    diag["validation"] = {"dropped_evidence_ids": sorted(set(dropped_ids)), "notes": notes,
                          "overrides": overrides}
    return diag


def empty_summary(reason):
    return validate({
        "headline": f"Investigation ended without a diagnosis ({reason}).",
        "affected_services": [],
        "anomalies": [],
        "root_causes": [],
        "suspected_origin": {"service": "unknown", "reasoning": reason, "confidence": 0.0, "evidence": []},
        "proposed_action": {"action": ESCALATE, "target": "unknown", "reason": reason},
        "open_questions": [],
    })
