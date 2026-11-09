"""The submit_diagnosis tool, root-cause validation and the remediation plan.

Checkpoint 2 output is a full DIAGNOSIS in JSON schema:
  - affected services & anomalies with numbers vs baseline
  - root causes with component, fault_class, confidence, and cited evidence IDs
  - proposed safe remediation from allowlisted runbook actions
  - an ordered remediation plan; each step is approved and run by oncall/remediate.py
  - graceful escalation when confidence is low or evidence is insufficient
"""
import re

from oncall import config
from oncall.agent.actions import AVAILABLE_ACTIONS, ESCALATE, _escalation, build_plan, depends_on, prevention_for
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

# The kinds of evidence (registry "kind") a fault class needs. A label cited without any is a guess.
NEEDS_EVIDENCE = {
    "container_stopped_or_restarting": ("container", "deploy"),
    "memory_leak": ("metric", "container", "deploy"),
    "cpu_or_gc_pressure": ("metric", "container", "deploy"),
    "resource_starvation": ("metric", "container", "deploy"),
    "traffic_flood": ("metric",),
    "network_degradation": ("trace", "metric"),
    "input_specific_error": ("trace", "log"),
}
UNEXPLAINED = "unexplained alert"
MAX_SUGGESTIONS, MAX_FIX_STEPS = 5, 8
# Classes a caller shows when the real failure is further down the call chain.
PROPAGATED = ("service_error", "service_unreachable", "dependency_down", "unknown")
# "Unreachable" without ever looking at the target's container state is only an inference.
UNCONFIRMED = {"service_unreachable": ("container", "deploy")}
UNCONFIRMED_CEILING = 0.75

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
                     "proposed_action", "suggested_fixes", "prevention", "open_questions"],
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
            "suggested_fixes": {
                "type": "array",
                "description": "Your own better fix, as advice for a human. Never executed. Empty if the allowlisted remedy is best.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["component", "fix", "why_better", "evidence"],
                    "properties": {
                        "component": {"type": "string", "description": "The root cause it fixes."},
                        "fix": {"type": "string", "description": "The fix, in one sentence."},
                        "steps": {"type": "array", "items": {"type": "string"}, "description": "What a person would do, in order."},
                        "why_better": {"type": "string", "description": "Why this beats the allowlisted remedy."},
                        "risk": {"type": "string", "description": "What could go wrong."},
                        "confidence": {"type": "number", "description": "0 to 1, that this fix would work."},
                        "evidence": _EVIDENCE,
                    },
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


def _as_dict(value, label, notes, string_key=None):
    """The model's value as a dict: a dict as is, a string as {string_key: value}, else {} (noted)."""
    if isinstance(value, dict):
        return dict(value)
    if value is None:
        return {}
    if isinstance(value, str) and string_key:
        notes.append(f"{label} was a string; read as {{{string_key!r}: {value[:60]!r}}}")
        return {string_key: value}
    notes.append(f"{label} had an unexpected type ({type(value).__name__}); ignored")
    return {}


def _as_list(value, label, notes):
    """The model's value as a list: a list as is, a single item wrapped, None as []."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    notes.append(f"{label} was not a list ({type(value).__name__}); wrapped")
    return [value]


def _key(name):
    text = re.sub(r"[^a-z0-9]", "", name.lower().replace("oteldemo.", ""))
    return text[:-7] if text.endswith("service") and len(text) > 7 else text


def canonical(name):
    """The shop's own name for a service: CurrencyService and "currency service" become currency."""
    if not isinstance(name, str):
        return name
    known = set(config.CRITICALITY) | set(config.DEPENDENCIES) | {s for v in config.DEPENDENCIES.values() for s in v}
    return {_key(s): s for s in known}.get(_key(name), name.strip())


def _without_victims(causes, affected, notes):
    """Drop a "root cause" that is only a caller of another one and shares its evidence."""
    kept = []
    for rc in causes:
        name = rc.get("component")
        origin = next((o for o in causes if o is not rc and o.get("status") == "active" and isinstance(name, str)
                       and isinstance(o.get("component"), str) and depends_on(name, o["component"])
                       and set(o["evidence"]) & set(rc["evidence"])), None)
        if origin is None or rc.get("fault_class") not in PROPAGATED:
            kept.append(rc)
            continue
        notes.append(f"{name} is a victim of {origin['component']}, not a separate root cause")
        if name not in {a.get("service") for a in affected}:
            affected.append({"service": name, "impact": f"fails because of {origin['component']}", "evidence": rc["evidence"]})
    return kept


def _text(value, limit=600):
    return value.strip()[:limit] if isinstance(value, str) else ""


def _suggestion(item):
    """A suggested fix with only known fields, as plain text. It is never added to the plan."""
    steps = item.get("steps") if isinstance(item.get("steps"), list) else []
    return {"component": _text(item.get("component"), 80) or "unknown", "fix": _text(item.get("fix")),
            "steps": [s for s in (_text(x) for x in steps[:MAX_FIX_STEPS]) if s],
            "why_better": _text(item.get("why_better")), "risk": _text(item.get("risk")) or "not assessed",
            "confidence": round(_clamp(item.get("confidence", 0.0)), 2), "evidence": item["evidence"]}


def _ids(value):
    """Evidence IDs as a list of strings (a single ID string is accepted)."""
    if isinstance(value, str):
        return [value]
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def validate(diag):
    """Drop claims without valid evidence; synthesizes root_causes and suspected_origin,

    validates proposed remediation, and calculates status (diagnosed vs escalate).
    Every field from the model is type-checked: a wrong type is coerced or dropped, never a crash.
    """
    notes, dropped_ids = [], []
    diag = _as_dict(diag, "diagnosis", notes)
    diag["proposed_action"] = _as_dict(diag.get("proposed_action"), "proposed_action", notes, "action")
    for key in ("prevention", "open_questions"):
        diag[key] = [x for x in _as_list(diag.get(key), key, notes) if isinstance(x, str)]

    def keep_valid(items, label):
        kept = []
        for item in _as_list(items, label, notes):
            if not isinstance(item, dict):
                notes.append(f"dropped {label} that is not an object: {str(item)[:60]}")
                continue
            valid, dropped = registry.validate(_ids(item.get("evidence")))
            dropped_ids.extend(dropped)
            if not valid:
                notes.append(f"dropped {label} without valid evidence: {item.get('service') or item.get('component')}")
                continue
            names = {k: canonical(item[k]) for k in ("service", "component") if k in item}
            kept.append({**item, **names, "evidence": valid})
        return kept

    diag["affected_services"] = keep_valid(diag.get("affected_services"), "affected service")
    diag["anomalies"] = keep_valid(diag.get("anomalies"), "anomaly")
    diag["ruled_out"] = keep_valid(diag.get("ruled_out"), "ruled-out suspect")
    diag["suggested_fixes"] = [s for s in map(_suggestion, keep_valid(diag.get("suggested_fixes"), "suggested fix"))
                               if s["fix"]][:MAX_SUGGESTIONS]

    # Validate root_causes
    raw_rcs = _as_list(diag.get("root_causes"), "root_causes", notes)
    valid_rcs = []
    for rc in raw_rcs:
        if not isinstance(rc, dict):
            notes.append(f"dropped root cause that is not an object: {str(rc)[:60]}")
            continue
        valid_ev, dropped = registry.validate(_ids(rc.get("evidence")))
        dropped_ids.extend(dropped)
        if not valid_ev:
            notes.append(f"dropped root cause without valid evidence: {rc.get('component')}")
            continue
        rc_clean = dict(rc)
        rc_clean["component"] = canonical(rc.get("component"))
        rc_clean["evidence"] = valid_ev
        rc_clean["confidence"] = round(_clamp(rc_clean.get("confidence", 0.0)), 2)
        rc_clean["evidence_detail"] = {e: registry.get(e)["summary"] for e in valid_ev if registry.get(e)}
        if rc_clean.get("fault_class") not in FAULT_CLASSES:
            rc_clean["fault_class"] = "unknown"
        needed = NEEDS_EVIDENCE.get(rc_clean["fault_class"])
        if (needed and rc_clean["confidence"] >= CONFIDENT
                and not any(registry.get(e)["kind"] in needed for e in valid_ev)):
            rc_clean["confidence"] = round(CONFIDENT - 0.01, 2)
            notes.append(f"root cause {rc_clean.get('component')}: {rc_clean['fault_class']} cites no "
                         f"{' or '.join(needed)} evidence; confidence capped below {CONFIDENT}")
        unconfirmed = UNCONFIRMED.get(rc_clean["fault_class"])
        if (unconfirmed and rc_clean["confidence"] > UNCONFIRMED_CEILING
                and not any(registry.get(e)["kind"] in unconfirmed for e in valid_ev)):
            rc_clean["confidence"] = UNCONFIRMED_CEILING
            notes.append(f"root cause {rc_clean.get('component')}: its container state was not cited, "
                         f"so confidence is limited to {UNCONFIRMED_CEILING}")
        if rc_clean.get("status") not in ROOT_CAUSE_STATUS:
            if rc_clean.get("status") is not None:
                notes.append(f"root cause {rc_clean.get('component')}: status {rc_clean['status']!r} "
                             f"is not active/resolved; treated as active")
            rc_clean["status"] = "active"
        valid_rcs.append(rc_clean)

    valid_rcs = _without_victims(valid_rcs, diag["affected_services"], notes)

    # Validate or synthesize suspected_origin
    origin = _as_dict(diag.get("suspected_origin"), "suspected_origin", notes, "service")
    if "service" in origin:
        origin["service"] = canonical(origin["service"])
    origin_ev, dropped = registry.validate(_ids(origin.get("evidence")))
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


def check_coverage(diag, alert):
    """Escalate every alerting service the diagnosis does not account for: it may be a second fault.
    Accounted for means a root cause, a caller of one (a victim), or ruled out with evidence. Steps
    already planned stay; each still needs its own human approval."""
    alert = alert if isinstance(alert, dict) else {}
    services = alert.get("alerting_services") or ([alert["service"]] if alert.get("service") else [])
    causes = diag.get("root_causes") or []
    if not causes or any(rc.get("fault_class") == "traffic_flood" for rc in causes):
        return []  # nothing to compare against / a flood explains every service it reaches
    origins = {rc.get("component") for rc in causes}
    cleared = {item.get("service") for item in diag.get("ruled_out") or []}
    missing = [s for s in dict.fromkeys(services)
               if s not in origins and s not in cleared and not any(depends_on(s, o) for o in origins)]
    plan = diag["remediation_plan"]
    for service in missing:
        reason = (f"{service} alerted, but no root cause explains it: it is not one of them and does not "
                  f"depend on {', '.join(sorted(o for o in origins if o))}. It may be a separate fault.")
        plan.setdefault("escalations", []).append({**_escalation(service, "unknown", reason), "source": UNEXPLAINED})
        diag["validation"]["notes"].append(reason)
        diag["open_questions"].append(f"What is wrong with {service}? Its alert is not explained by this diagnosis.")
    return missing


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
