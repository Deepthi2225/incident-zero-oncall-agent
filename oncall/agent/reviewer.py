"""Optional reviewer agent (organisers' stretch goal 2): a skeptical second opinion on the diagnosis.

After submit_diagnosis is validated and before the plan is shown for approval, ONE model call (same
provider as the agent, only the submit_review tool) tries to DISPROVE the diagnosis from the
registered evidence alone.

What the reviewer can and cannot do is enforced here in code, not only in the prompt:
  - it can only LOWER confidence: the adjustment is clamped to [-0.3, 0];
  - it never adds, changes or retargets a step and approves nothing; the plan can only shrink:
    root causes that fall below the threshold are escalated with the objections attached;
  - objections citing evidence IDs that are not in the registry are dropped;
  - a suggested alternative root cause is shown to the human only, never acted on;
  - the kill switch is checked before the call; any error or timeout leaves the diagnosis exactly
    as it was and records "review skipped: <reason>".
"""
import json
import threading
import time

from oncall import config
from oncall.agent.actions import ESCALATE, NEXT_STEPS
from oncall.evidence import registry

VERDICTS = ("agree", "doubt", "disagree")
MAX_DROP = 0.3
MAX_EVIDENCE_CHARS = 40000

REVIEW_TOOL = {
    "name": "submit_review",
    "description": "Submit your review of the diagnosis. Call exactly once.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["verdict", "objections", "confidence_adjustment"],
        "properties": {
            "verdict": {"type": "string", "enum": list(VERDICTS)},
            "objections": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["text", "evidence"],
                "properties": {"text": {"type": "string"},
                               "evidence": {"type": "array", "items": {"type": "string"},
                                            "description": "Evidence IDs that support this objection."}}}},
            "alternative_root_cause": {
                "type": "object", "description": "Optional. Shown to the human only, never acted on.",
                "properties": {"component": {"type": "string"}, "fault_class": {"type": "string"},
                               "reason": {"type": "string"},
                               "evidence": {"type": "array", "items": {"type": "string"}}}},
            "confidence_adjustment": {"type": "number",
                                      "description": "Between -0.3 and 0. You can only lower confidence."},
        },
    },
}

SYSTEM = """You are a skeptical senior SRE reviewing another engineer's incident diagnosis. Try to \
DISPROVE it, using ONLY the evidence listed below. You have no tools and no other data.

Check in particular:
- victim versus origin: is a named root cause only a victim of a failing dependency or caller?
- evidence that contradicts a claim (for example the container is running and serving);
- alternative causes that the evidence does not rule out;
- claims that their cited evidence IDs do not actually support.

Rules:
- Every objection must cite the evidence IDs it rests on. Objections without valid IDs are discarded.
- You cannot change the plan, the actions or the targets, and you approve nothing. You can only lower \
confidence, by 0 to 0.3 (confidence_adjustment between -0.3 and 0).
- If the diagnosis holds up, answer verdict "agree" with adjustment 0. Use "doubt" for real but \
limited concerns, "disagree" when the evidence points elsewhere or contradicts the claim.
- Call submit_review exactly once."""


def _clamp_adjustment(value):
    try:
        return max(-MAX_DROP, min(0.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _message(alert, diag):
    evidence = "\n".join(f"{e['id']} [{e.get('tool')}] {e.get('summary', '')}" for e in registry.all())
    view = json.loads(json.dumps({k: diag.get(k) for k in ("headline", "status", "confidence", "root_causes",
                                                            "ruled_out", "suspected_origin")}, default=str))
    for rc in view.get("root_causes") or []:  # a copy: the diagnosis itself is not touched here
        rc.pop("evidence_detail", None)
    return ("ALERT:\n```json\n" + json.dumps(alert, default=str, indent=1)[:4000] + "\n```\n\n"
            "DIAGNOSIS TO REVIEW:\n```json\n" + json.dumps(view, default=str, indent=1) + "\n```\n\n"
            "REGISTERED EVIDENCE (the only data you may use):\n" + evidence[:MAX_EVIDENCE_CHARS])


def _call_with_timeout(llm, messages, timeout_s):
    """The provider call on a worker thread; (response, error). A late answer is ignored."""
    box = {}

    def target():
        try:
            box["response"] = llm.create(SYSTEM, [REVIEW_TOOL], messages)
        except Exception as e:  # any provider failure means "skipped", never a broken run
            box["error"] = f"{type(e).__name__}: {e}"

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        return None, f"timeout after {timeout_s:g}s"
    return box.get("response"), box.get("error")


def _skipped(reason, started, confidence):
    return {"status": "skipped", "reason": f"review skipped: {reason}", "verdict": None, "objections": [],
            "dropped_objections": 0, "alternative_root_cause": None, "adjustment": 0.0,
            "confidence_before": confidence, "confidence_after": confidence, "escalated": False,
            "tokens_in": 0, "tokens_out": 0, "seconds": round(time.time() - started, 2)}


def review_diagnosis(diag, alert, llm, threshold, on_tokens=None, timeout_s=None, log=print):
    """Review a validated diagnosis in place. Returns the review record (also stored as diag["review"])."""
    started = time.time()
    before = diag.get("confidence", 0.0)
    if config.KILL_SWITCH_FILE.exists():
        review = _skipped("kill switch is on", started, before)
    else:
        timeout_s = config.REVIEWER_TIMEOUT_S if timeout_s is None else timeout_s
        response, error = _call_with_timeout(llm, [{"role": "user", "content": _message(alert, diag)}], timeout_s)
        if response is not None and on_tokens:
            on_tokens(response.usage.input_tokens, response.usage.output_tokens)
        if error or response is None:
            review = _skipped(error or "no response", started, before)
        else:
            call = next((b for b in response.content if b.type == "tool_use" and b.name == REVIEW_TOOL["name"]), None)
            if call is None:
                review = _skipped("the reviewer returned no structured review", started, before)
            else:
                try:
                    review = _apply(diag, call.input or {}, threshold, before, started)
                except Exception as e:  # malformed review: keep the diagnosis exactly as it was
                    review = _skipped(f"invalid review ({type(e).__name__}: {e})", started, before)
                review["tokens_in"] = response.usage.input_tokens
                review["tokens_out"] = response.usage.output_tokens
    review["seconds"] = round(time.time() - started, 2)
    diag["review"] = review
    if review["status"] == "skipped":
        log(f"[review] {review['reason']}")
    else:
        log(f"[review] {review['verdict']}: confidence {review['confidence_before']} -> "
            f"{review['confidence_after']}" + (" -> ESCALATED" if review["escalated"] else ""))
    return review


def _apply(diag, raw, threshold, before, started):
    """Compute everything first, then change diag in one go (so a failure leaves it untouched)."""
    verdict = raw.get("verdict") if raw.get("verdict") in VERDICTS else "doubt"
    adjustment = _clamp_adjustment(raw.get("confidence_adjustment", 0))
    objections, dropped = [], 0
    for o in raw.get("objections") or []:
        valid, bad = registry.validate((o or {}).get("evidence"))
        if not valid or not str(o.get("text", "")).strip():
            dropped += 1
            continue
        objections.append({"text": str(o["text"]).strip(), "evidence": valid})
    alt = raw.get("alternative_root_cause")
    if isinstance(alt, dict) and alt.get("component"):
        alt = {"component": alt.get("component"), "fault_class": alt.get("fault_class"),
               "reason": alt.get("reason", ""), "evidence": registry.validate(alt.get("evidence"))[0],
               "note": "shown to the human only; never acted on"}
    else:
        alt = None

    root_causes = [dict(rc, confidence=round(max(0.0, rc["confidence"] + adjustment), 2))
                   for rc in diag.get("root_causes") or []]
    after = round(max([rc["confidence"] for rc in root_causes], default=max(0.0, before + adjustment)), 2)
    plan = diag.get("remediation_plan") or {"steps": [], "escalations": []}
    low = {rc["component"] for rc in root_causes if rc["confidence"] < threshold}
    overall_low = after < threshold
    kept = [s for s in plan["steps"] if not overall_low and s.get("component", s["target"]) not in low]
    removed = [s for s in plan["steps"] if s not in kept]
    assert all(s in plan["steps"] for s in kept)  # the reviewer can only remove steps, never add or change

    extra = [o["text"] for o in objections[:3]]
    escalations = list(plan.get("escalations", []))
    for s in removed:
        escalations.append({"action": ESCALATE, "target": s.get("component", s["target"]),
                            "fault_class": s.get("fault_class"),
                            "reason": (f"reviewer lowered confidence to {after} (below {threshold}); "
                                       f"escalating instead of {s['action']} on {s['target']}"),
                            "next_steps": [f"Reviewer objection: {t}" for t in extra]
                                          + NEXT_STEPS.get(s.get("fault_class"), NEXT_STEPS["unknown"])})

    # apply
    diag["root_causes"] = root_causes
    diag["confidence"] = after
    if overall_low and diag.get("status") == "diagnosed":
        diag["status"] = "escalate"
    for i, s in enumerate(kept, 1):
        s["step"] = i
    plan["steps"], plan["escalations"] = kept, escalations
    diag["remediation_plan"] = plan
    if removed:
        diag["proposed_action"] = dict((kept or escalations)[0])
    note = f"reviewer {verdict}: confidence {before} -> {after}"
    if removed:
        note += f"; {len(removed)} step(s) escalated instead"
    diag.setdefault("validation", {}).setdefault("notes", []).append(note)
    return {"status": "done", "reason": None, "verdict": verdict, "objections": objections,
            "dropped_objections": dropped, "alternative_root_cause": alt, "adjustment": adjustment,
            "confidence_before": before, "confidence_after": after, "escalated": bool(removed) or overall_low,
            "tokens_in": 0, "tokens_out": 0, "seconds": round(time.time() - started, 2)}


def reviewer_line(diag):
    """One line for the approval box, or None when no review ran."""
    review = (diag or {}).get("review")
    if not review:
        return None
    if review["status"] == "skipped":
        return review["reason"]
    if review["verdict"] == "agree":
        return "agrees"
    top = review["objections"][0]["text"] if review["objections"] else "no objection with valid evidence"
    return f"{review['verdict']}: {top[:150]}"
