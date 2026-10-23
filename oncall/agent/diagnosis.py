"""The submit_diagnosis tool and the validator that enforces evidence rules.

NOTE: this is our provisional internal shape. When the organisers' JSON schema arrives, map it in
to_output_schema() and keep everything else unchanged.
"""
from oncall.evidence import registry

ESCALATE_BELOW = 0.6

FAULT_CLASSES = [
    "service_error", "service_unreachable", "input_specific_error", "memory_leak",
    "cpu_or_gc_pressure", "traffic_flood", "container_stopped_or_restarting",
    "dependency_down", "network_degradation", "resource_starvation", "unknown",
]
ACTIONS = ["restart_service", "toggle_feature_flag", "throttle_load_generator", "scale_service",
           "none"]

_EVIDENCE = {"type": "array", "items": {"type": "string"},
             "description": "Evidence IDs from tool results, e.g. [\"E3\", \"E7\"]."}

SUBMIT_TOOL = {
    "name": "submit_diagnosis",
    "description": "Submit the final diagnosis. Call exactly once, at the end.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", "summary", "symptoms", "root_causes", "affected_services",
                     "confidence", "proposed_action", "escalation_reason", "next_checks"],
        "properties": {
            "status": {"type": "string", "enum": ["diagnosed", "escalate"]},
            "summary": {"type": "string", "description": "Two or three sentences for a human."},
            "symptoms": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["description", "evidence"],
                "properties": {"description": {"type": "string"}, "evidence": _EVIDENCE}}},
            "root_causes": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["component", "fault_class", "cause", "confidence", "evidence"],
                "properties": {
                    "component": {"type": "string", "description": "Service or container name."},
                    "fault_class": {"type": "string", "enum": FAULT_CLASSES},
                    "cause": {"type": "string"},
                    "confidence": {"type": "number", "description": "0 to 1."},
                    "evidence": _EVIDENCE}}},
            "affected_services": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number", "description": "Overall, 0 to 1."},
            "proposed_action": {
                "type": "object", "additionalProperties": False,
                "required": ["action", "target", "reason"],
                "properties": {"action": {"type": "string", "enum": ACTIONS},
                               "target": {"type": "string"},
                               "reason": {"type": "string"}}},
            "escalation_reason": {"type": "string", "description": "Empty if not escalating."},
            "next_checks": {"type": "array", "items": {"type": "string"},
                            "description": "What a human should verify next."},
        },
    },
}


def _clamp(x):
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return 0.0


def validate(diag):
    """Drop uncited claims, clamp confidences, and escalate when support is weak.

    Returns the cleaned diagnosis with a "validation" section describing what was removed.
    """
    diag = dict(diag)
    notes = []
    dropped_ids = []

    def keep_valid(items, label):
        kept = []
        for item in items or []:
            valid, dropped = registry.validate(item.get("evidence"))
            dropped_ids.extend(dropped)
            if not valid:
                notes.append(f"removed {label} without valid evidence: "
                             f"{item.get('component') or item.get('description')}")
                continue
            kept.append({**item, "evidence": valid})
        return kept

    diag["symptoms"] = keep_valid(diag.get("symptoms"), "symptom")
    diag["root_causes"] = keep_valid(diag.get("root_causes"), "root cause")
    for rc in diag["root_causes"]:
        rc["confidence"] = _clamp(rc.get("confidence"))
        rc["evidence_detail"] = [registry.get(e)["summary"] for e in rc["evidence"]]

    confidence = _clamp(diag.get("confidence"))
    if diag["root_causes"]:
        confidence = min(confidence, max(rc["confidence"] for rc in diag["root_causes"]))
    else:
        confidence = min(confidence, 0.3)
    diag["confidence"] = round(confidence, 2)

    if diag.get("status") == "diagnosed" and (not diag["root_causes"] or confidence < ESCALATE_BELOW):
        diag["status"] = "escalate"
        reason = "no root cause survived evidence validation" if not diag["root_causes"] \
            else f"confidence {confidence:.2f} below {ESCALATE_BELOW}"
        diag["escalation_reason"] = (diag.get("escalation_reason") or "") + \
            (" | " if diag.get("escalation_reason") else "") + reason
        notes.append(f"status changed to escalate: {reason}")

    action = diag.get("proposed_action") or {"action": "none", "target": "", "reason": ""}
    if action.get("action") not in ACTIONS:
        action = {"action": "none", "target": "", "reason": "invalid action removed"}
    diag["proposed_action"] = action
    diag["validation"] = {"dropped_evidence_ids": sorted(set(dropped_ids)), "notes": notes}
    return diag


def to_output_schema(diag, alert, run_id):
    """Map our diagnosis to the organisers' required JSON. TODO: fill in once schema is known."""
    return {"run_id": run_id, "alert": alert, **diag}
