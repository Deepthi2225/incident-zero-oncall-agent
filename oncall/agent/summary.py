"""The submit_summary tool and the validator that enforces evidence rules.

Checkpoint 1 output is a SYMPTOM SUMMARY: affected services, anomalies with numbers, the suspected
origin and the evidence IDs behind each claim. Any claim whose evidence IDs are not in the registry
is dropped. (Checkpoint 2 extends this into the full diagnosis in the organisers' JSON schema.)
"""
from oncall.evidence import registry

CONFIDENT = 0.6

_EVIDENCE = {"type": "array", "items": {"type": "string"},
             "description": "Evidence IDs from tool results, e.g. [\"E3\", \"E7\"]."}

SUBMIT_TOOL = {
    "name": "submit_summary",
    "description": "Submit the symptom summary. Call exactly once, at the end.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["headline", "affected_services", "anomalies", "suspected_origin",
                     "open_questions"],
        "properties": {
            "headline": {"type": "string", "description": "One sentence for the on-call human."},
            "affected_services": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["service", "impact", "evidence"],
                "properties": {"service": {"type": "string"},
                               "impact": {"type": "string", "description": "What users or callers see."},
                               "evidence": _EVIDENCE}}},
            "anomalies": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["service", "signal", "observed", "baseline", "evidence"],
                "properties": {
                    "service": {"type": "string"},
                    "signal": {"type": "string", "description": "e.g. error_rate, p95, container_state, log_errors"},
                    "observed": {"type": "string", "description": "The number or state now, copied from the evidence."},
                    "baseline": {"type": "string", "description": "The healthy number, or 'unknown'."},
                    "evidence": _EVIDENCE}}},
            "suspected_origin": {
                "type": "object", "additionalProperties": False,
                "required": ["service", "reasoning", "confidence", "evidence"],
                "properties": {
                    "service": {"type": "string", "description": "Where the problem starts, or 'unknown'."},
                    "reasoning": {"type": "string"},
                    "confidence": {"type": "number", "description": "0 to 1."},
                    "evidence": _EVIDENCE}},
            "open_questions": {"type": "array", "items": {"type": "string"},
                               "description": "What is still unexplained or should be checked next."},
        },
    },
}


def _clamp(x):
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return 0.0


def validate(summary):
    """Drop claims without valid evidence; returns the cleaned summary with a validation report."""
    summary = dict(summary)
    notes, dropped_ids = [], []

    def keep_valid(items, label):
        kept = []
        for item in items or []:
            valid, dropped = registry.validate(item.get("evidence"))
            dropped_ids.extend(dropped)
            if not valid:
                notes.append(f"dropped {label} without valid evidence: {item.get('service')}")
                continue
            kept.append({**item, "evidence": valid})
        return kept

    summary["affected_services"] = keep_valid(summary.get("affected_services"), "affected service")
    summary["anomalies"] = keep_valid(summary.get("anomalies"), "anomaly")

    origin = dict(summary.get("suspected_origin") or {})
    valid, dropped = registry.validate(origin.get("evidence"))
    dropped_ids.extend(dropped)
    if valid:
        origin["evidence"] = valid
        origin["confidence"] = round(_clamp(origin.get("confidence")), 2)
        origin["evidence_detail"] = {e: registry.get(e)["summary"] for e in valid}
    else:
        if origin.get("service") not in (None, "", "unknown"):
            notes.append(f"dropped suspected origin without valid evidence: {origin.get('service')}")
        origin = {"service": "unknown", "reasoning": origin.get("reasoning", ""),
                  "confidence": 0.0, "evidence": []}
    summary["suspected_origin"] = origin
    summary["status"] = "origin_suspected" if origin["confidence"] >= CONFIDENT else "needs_human"
    summary["validation"] = {"dropped_evidence_ids": sorted(set(dropped_ids)), "notes": notes}
    return summary


def empty_summary(reason):
    return validate({"headline": f"Investigation ended without a summary ({reason}).",
                     "affected_services": [], "anomalies": [],
                     "suspected_origin": {"service": "unknown", "reasoning": reason,
                                          "confidence": 0, "evidence": []},
                     "open_questions": []})


def render(s):
    o = s["suspected_origin"]
    source = s.get("source", "?")
    lines = [f"\n--- SYMPTOM SUMMARY [{s['status']}] source={source} ({s.get('source_detail', '')})"]
    if source == "test":
        lines.append("!!! TEST MODE: fixture data, not the live demo")
    lines += [s.get("headline", ""), "Affected:"]
    lines += [f"  - {a['service']}: {a['impact']} [{', '.join(a['evidence'])}]"
              for a in s["affected_services"]]
    lines.append("Anomalies:")
    lines += [f"  - {a['service']} {a['signal']}: {a['observed']} (baseline {a['baseline']}) "
              f"[{', '.join(a['evidence'])}]" for a in s["anomalies"]]
    lines.append(f"Suspected origin: {o['service']} (confidence {o['confidence']}) - {o.get('reasoning', '')}")
    lines += [f"    {e}: {text[:170]}" for e, text in o.get("evidence_detail", {}).items()]
    if s.get("open_questions"):
        lines.append("Open questions:")
        lines += [f"  - {q}" for q in s["open_questions"]]
    lines += [f"(validator) {n}" for n in s["validation"]["notes"]]
    return "\n".join(lines)
