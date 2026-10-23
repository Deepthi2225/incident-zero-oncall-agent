"""Agent run: the LLM picks the next read-only tool until it can submit a symptom summary.

Input is an alert JSON. Prints every tool call with a one-line result live, enforces the step cap
(config.MAX_STEPS, default 10) and the kill switch, drops claims whose evidence IDs are not in the
registry, and saves the whole run to runs/<timestamp>.json.

CLI: python -m oncall.agent.run alert.json                       (live, the default)
     python -m oncall.agent.run tests/fixtures/payment_down/alert.json --test
"""
import argparse
import json
import time
from datetime import datetime
from pathlib import Path

import anthropic

from oncall import config
from oncall.agent.prompt import alert_message, build_system_prompt
from oncall.agent.summary import SUBMIT_TOOL, empty_summary, render, validate
from oncall.evidence import registry
from oncall.sources import add_source_args, apply_source_args, source_info
from oncall.tools.catalog import TOOL_SPECS, dispatch

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class KillSwitch(RuntimeError):
    pass


class NoCredentials(RuntimeError):
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
    first = registry.get(ids[0])["summary"][:110] if ids else ""
    return (f"{sizes or 'ok'} | evidence {', '.join(ids[:6])}{'...' if len(ids) > 6 else ''}"
            + (f" | {first}" if first else ""))


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


class Agent:
    def __init__(self, model=None, max_steps=None, verbose=True):
        self.client = anthropic.Anthropic()
        self.model = model or config.MODEL
        self.max_steps = max_steps or config.MAX_STEPS
        self.verbose = verbose
        self.use_fallbacks = config.USE_FALLBACKS
        self.tools = TOOL_SPECS + [SUBMIT_TOOL]
        self.system = build_system_prompt(self.max_steps)

    def log(self, text):
        if self.verbose:
            print(text, flush=True)

    def _call_model(self, messages):
        try:
            return self._create(messages)
        except TypeError as e:  # the SDK raises TypeError when no API key or token is configured
            if "authentication" not in str(e):
                raise
            raise NoCredentials("no LLM credentials: set ANTHROPIC_API_KEY") from e

    def _create(self, messages):
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
        info = source_info()
        record = {"run_id": run_id, **info, "alert": alert, "model": self.model,
                  "steps": [], "usage": {"input": 0, "output": 0}}
        messages = [{"role": "user", "content": alert_message(alert)}]
        summary, outcome = None, "incomplete"
        if info["source"] == "test":
            self.log("!!! TEST MODE: answers come from fixtures, not the live demo")
        self.log(f"=== run {run_id} | source={info['source']} ({info['source_detail']}) | alert: "
                 f"{json.dumps(alert, default=str)[:200]}")

        try:
            for step in range(1, self.max_steps + 1):
                check_kill_switch()
                if step == self.max_steps:
                    _add_user_text(messages, "Step budget exhausted. Call submit_summary now with "
                                   "what you have; use origin 'unknown' if it is not clear.")
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
                                     "Use the tools to continue, or call submit_summary."})
                    continue

                submit = next((b for b in tool_uses if b.name == SUBMIT_TOOL["name"]), None)
                if submit:
                    summary = validate(submit.input)
                    outcome = "submitted"
                    self.log(f"[{step}] -> {SUBMIT_TOOL['name']}")
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
        except NoCredentials as e:
            outcome = "llm_error"
            record["error"] = str(e)
            self.log(f"!!! {e}")
        except anthropic.APIError as e:
            outcome = "llm_error"
            record["error"] = f"{type(e).__name__}: {e}"
            self.log(f"!!! LLM error: {record['error'][:300]}")

        if summary is None:
            summary = empty_summary(f"agent outcome: {outcome}")
        summary = {**info, **summary}

        record.update(outcome=outcome, seconds=round(time.time() - started, 1),
                      evidence=registry.dump(), summary=summary)
        config.RUNS_DIR.mkdir(parents=True, exist_ok=True)
        suffix = "" if info["source"] == "live" else f"-{info['source']}"
        path = config.RUNS_DIR / f"{run_id}{suffix}.json"
        path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
        record["path"] = str(path)
        self.log(render(summary))
        self.log(f"=== saved {path}")
        return record


def run_agent(alert, **kwargs):
    return Agent(**kwargs).run(alert)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("alert", help="alert JSON file")
    add_source_args(p)
    p.add_argument("--max-steps", type=int)
    p.add_argument("--model")
    args = p.parse_args()
    apply_source_args(p, args)
    alert = json.loads(Path(args.alert).read_text())
    record = run_agent(alert, model=args.model, max_steps=args.max_steps)
    raise SystemExit(0 if record["outcome"] == "submitted" else 1)


if __name__ == "__main__":
    main()
