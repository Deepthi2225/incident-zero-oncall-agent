"""Agent loop: LLM decides which read-only tool to call next, then submits a diagnosis.

Prints every tool call live, enforces the step cap and the kill switch, validates evidence,
and saves the whole run to runs/<timestamp>.json.
"""
import json
import time
from datetime import datetime

import anthropic

from oncall import config
from oncall.agent.diagnosis import SUBMIT_TOOL, to_output_schema, validate
from oncall.agent.prompt import alert_message, build_system_prompt
from oncall.evidence import registry
from oncall.sources import get_source
from oncall.tools.catalog import TOOL_SPECS, dispatch

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class KillSwitch(RuntimeError):
    pass


def check_kill_switch():
    if config.KILL_SWITCH_FILE.exists():
        raise KillSwitch(f"kill switch file present: {config.KILL_SWITCH_FILE}")


def _one_line(result, is_error):
    if is_error:
        return f"ERROR {result.get('error', '')[:120]}"
    ids = []

    def walk(x):
        if isinstance(x, dict):
            if isinstance(x.get("evidence"), str):
                ids.append(x["evidence"])
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(result)
    sizes = ", ".join(f"{k}={len(v)}" for k, v in result.items() if isinstance(v, list))
    return f"{sizes or 'ok'} | evidence {', '.join(ids[:8])}{'...' if len(ids) > 8 else ''}"


class Agent:
    def __init__(self, model=None, max_steps=None, verbose=True):
        self.client = anthropic.Anthropic()
        self.model = model or config.MODEL
        self.max_steps = max_steps or config.MAX_STEPS
        self.verbose = verbose
        self.use_fallbacks = config.USE_FALLBACKS
        self.tools = TOOL_SPECS + [SUBMIT_TOOL]
        self.system = build_system_prompt()

    def log(self, text):
        if self.verbose:
            print(text, flush=True)

    def _call_model(self, messages):
        kwargs = dict(
            model=self.model, max_tokens=16000, system=self.system, tools=self.tools,
            messages=messages,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": config.EFFORT},
        )
        if self.use_fallbacks:
            try:
                return self.client.beta.messages.create(
                    **kwargs, betas=[FALLBACK_BETA], extra_body={"fallbacks": "default"})
            except anthropic.BadRequestError as e:
                if "fallback" not in str(e).lower():
                    raise
                self.log("  (refusal fallbacks not accepted by this endpoint; continuing without)")
                self.use_fallbacks = False
        return self.client.beta.messages.create(**kwargs)

    def run(self, alert):
        registry.reset()
        started = time.time()
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        record = {"run_id": run_id, "alert": alert, "model": self.model,
                  "source": get_source().name, "steps": [], "usage": {"input": 0, "output": 0}}
        messages = [{"role": "user", "content": alert_message(alert)}]
        diagnosis, outcome = None, "incomplete"
        self.log(f"=== investigation {run_id} | alert: {json.dumps(alert, default=str)[:200]}")

        try:
            for step in range(1, self.max_steps + 1):
                check_kill_switch()
                if step == self.max_steps:
                    _add_user_text(messages, "Step budget exhausted. Call submit_diagnosis now "
                                   "with what you have; escalate if the evidence is not conclusive.")
                response = self._call_model(messages)
                record["usage"]["input"] += response.usage.input_tokens
                record["usage"]["output"] += response.usage.output_tokens

                if response.stop_reason == "refusal":
                    outcome = "refused"
                    break
                for block in response.content:
                    if block.type == "thinking" and block.thinking:
                        self.log(f"[{step}] thinking: {block.thinking.strip()[:200]}")
                    elif block.type == "text" and block.text.strip():
                        self.log(f"[{step}] {block.text.strip()[:300]}")

                messages.append({"role": "assistant", "content": response.content})
                tool_uses = [b for b in response.content if b.type == "tool_use"]
                if not tool_uses:
                    messages.append({"role": "user", "content":
                                     "Use the tools to continue, or call submit_diagnosis."})
                    continue

                submit = next((b for b in tool_uses if b.name == "submit_diagnosis"), None)
                if submit:
                    diagnosis = validate(submit.input)
                    outcome = "submitted"
                    self.log(f"[{step}] -> submit_diagnosis")
                    break

                results = []
                for tu in tool_uses:
                    check_kill_switch()
                    t0 = time.time()
                    result, is_error = dispatch(tu.name, tu.input)
                    took = time.time() - t0
                    args = ", ".join(f"{k}={v!r}" for k, v in (tu.input or {}).items())
                    self.log(f"[{step}] -> {tu.name}({args}) {took:.1f}s :: {_one_line(result, is_error)}")
                    record["steps"].append({"step": step, "tool": tu.name, "input": tu.input,
                                            "result": result, "is_error": is_error,
                                            "seconds": round(took, 2)})
                    results.append({"type": "tool_result", "tool_use_id": tu.id,
                                    "content": json.dumps(result, default=str), "is_error": is_error})
                messages.append({"role": "user", "content": results})
        except KillSwitch as e:
            outcome = "killed"
            self.log(f"!!! {e}")
        except anthropic.APIError as e:
            outcome = "llm_error"
            record["error"] = f"{type(e).__name__}: {e}"
            self.log(f"!!! LLM error: {record['error'][:300]}")

        if diagnosis is None:
            diagnosis = validate({
                "status": "escalate", "summary": f"Investigation ended without a diagnosis ({outcome}).",
                "symptoms": [], "root_causes": [], "affected_services": [], "confidence": 0,
                "proposed_action": {"action": "none", "target": "", "reason": ""},
                "escalation_reason": f"agent outcome: {outcome}", "next_checks": []})

        record.update(outcome=outcome, seconds=round(time.time() - started, 1),
                      evidence=registry.dump(), diagnosis=diagnosis,
                      output=to_output_schema(diagnosis, alert, run_id))
        config.RUNS_DIR.mkdir(parents=True, exist_ok=True)
        path = config.RUNS_DIR / f"{run_id}.json"
        path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
        record["path"] = str(path)
        self.log(_render(diagnosis))
        self.log(f"=== saved {path}")
        return record


def _add_user_text(messages, text):
    """Add text to the trailing user turn (e.g. after tool results) instead of a second user turn."""
    last = messages[-1]
    if last["role"] == "user":
        content = last["content"]
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        last["content"] = content + [{"type": "text", "text": text}]
    else:
        messages.append({"role": "user", "content": text})


def _render(d):
    lines = [f"\n--- DIAGNOSIS: {d['status'].upper()} (confidence {d['confidence']})", d.get("summary", "")]
    for rc in d["root_causes"]:
        lines.append(f"* {rc['component']} [{rc['fault_class']}] {rc['cause']} "
                     f"(conf {rc['confidence']}; evidence {', '.join(rc['evidence'])})")
        lines.extend(f"    {e}: {s[:160]}" for e, s in zip(rc["evidence"], rc["evidence_detail"]))
    a = d["proposed_action"]
    lines.append(f"Proposed action: {a['action']} {a.get('target', '')} - {a.get('reason', '')}")
    if d["status"] == "escalate":
        lines.append(f"Escalation: {d.get('escalation_reason', '')}")
    for note in d["validation"]["notes"]:
        lines.append(f"(validator) {note}")
    return "\n".join(lines)


def investigate(alert, **kwargs):
    return Agent(**kwargs).run(alert)
