"""Agent run: the LLM picks the next read-only tool until it can submit a diagnosis.

Input is an alert JSON. Prints every tool call with a one-line result live, enforces the step cap
(config.MAX_STEPS, default 10) and the kill switch, drops claims whose evidence IDs are not in the
registry, runs the remediation plan step by step (dry run unless --execute), and saves
the whole run to runs/<timestamp>.json.

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
from oncall.agent.diagnosis import SUBMIT_TOOL, empty_summary, validate
from oncall.action_api import DryRunClient
from oncall.record import RunRecord, unique_run_id
from oncall.remediate import make_approver, run_plan
from oncall.report import render_diagnosis, render_outcome
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
    def __init__(self, model=None, max_steps=None, verbose=True, auto_approve=False, interactive=None,
                 action_client=None, verifier=None):
        self.client = anthropic.Anthropic()
        self.model = model or config.MODEL
        self.max_steps = max_steps or config.MAX_STEPS
        self.verbose = verbose
        self.auto_approve = auto_approve
        self.interactive = (not auto_approve) if interactive is None else interactive
        # Dry run unless a live Action API client is passed in (--execute).
        self.action_client = action_client or DryRunClient()
        self.approve = make_approver(auto_approve, self.interactive)
        self.verifier = verifier
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
        """One incident: investigate -> diagnose -> plan step by step. Returns the incident record
        (exactly what is saved in runs/<run_id>.json) plus its "path"."""
        registry.reset()
        info = source_info()
        run_id = unique_run_id(datetime.now().strftime("%Y%m%d-%H%M%S"), info["source"])
        rec = RunRecord(run_id, alert, info, model=self.model, max_steps=self.max_steps)
        rec.save()  # phase: alert received
        try:
            self._investigate_and_act(rec, alert, info, run_id)
        finally:
            if rec.data["status"] == "running":  # Ctrl+C or a crash: leave a valid partial file
                rec.interrupt()
                self.log(f"!!! run interrupted; partial record saved to {rec.path}")
        return {**rec.saved, "path": str(rec.path)}

    def _investigate_and_act(self, rec, alert, info, run_id):
        started = rec.t0
        messages = [{"role": "user", "content": alert_message(alert)}]
        summary, outcome, error = None, "incomplete", None
        if info["source"] == "test":
            self.log("!!! TEST MODE: answers come from fixtures, not the live demo")
        self.log(f"=== run {run_id} | source={info['source']} ({info['source_detail']}) | alert: "
                 f"{json.dumps(alert, default=str)[:200]}")

        try:
            for step in range(1, self.max_steps + 1):
                check_kill_switch()
                if step == self.max_steps:
                    _add_user_text(messages, "Step budget exhausted. Call submit_diagnosis now with "
                                   "what you have; use origin/root cause 'unknown' if it is not clear.")
                response = self._call_model(messages)
                rec.model_turn(response.usage.input_tokens, response.usage.output_tokens)

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

                submit = next((b for b in tool_uses if b.name == SUBMIT_TOOL["name"]), None)
                if submit:
                    summary = validate(submit.input)
                    outcome = "submitted"
                    self.log(f"[{step}] -> {submit.name}")
                    break

                results = []
                for tu in tool_uses:
                    check_kill_switch()
                    t0 = time.time()
                    known = {e["id"] for e in registry.all()}
                    result, is_error = dispatch(tu.name, tu.input)
                    took = time.time() - t0
                    args = ", ".join(f"{k}={v!r}" for k, v in (tu.input or {}).items())
                    self.log(f"[{step}] -> {tu.name}({args}) {took:.1f}s :: {_one_line(result, is_error)}")
                    rec.tool_call(step=step, tool=tu.name, arguments=tu.input or {}, result=result,
                                  evidence_ids=[e["id"] for e in registry.all() if e["id"] not in known],
                                  seconds=round(took, 2), at_s=round(t0 - started, 1),
                                  error=result.get("error") if is_error else None)
                    results.append({"type": "tool_result", "tool_use_id": tu.id,
                                    "content": json.dumps(result, default=str), "is_error": is_error})
                messages.append({"role": "user", "content": results})
        except KillSwitch as e:
            outcome, error = "killed", str(e)
            self.log(f"!!! {e}")
        except NoCredentials as e:
            outcome, error = "llm_error", str(e)
            self.log(f"!!! {e}")
        except anthropic.APIError as e:
            outcome, error = "llm_error", f"{type(e).__name__}: {e}"
            self.log(f"!!! LLM error: {error[:300]}")

        rec.investigation_done(outcome, error)
        if summary is None:
            summary = empty_summary(f"agent outcome: {outcome}")
        rec.set_diagnosis(summary)  # phase: diagnosis
        self.log(render_diagnosis(rec.saved))

        if outcome == "submitted":  # one step at a time: approve -> act -> verify
            try:
                run_plan(summary, self.action_client, self.approve, self._verifier(), run_id, self.log,
                         on_update=rec.plan_updated)
            except Exception as e:  # never lose the diagnosis because the plan failed
                summary["remediation_plan"]["error"] = f"{type(e).__name__}: {e}"
                self.log(f"!!! remediation plan error: {e}")
        final = rec.finish()  # phase: outcome (also writes the report and the index line)
        self.log(render_outcome(final))
        self.log(f"=== saved {rec.path}")

    def _verifier(self):
        if self.verifier is None:
            from oncall.verify import Verifier
            self.verifier = Verifier()
        return self.verifier


def run_agent(alert, **kwargs):
    return Agent(**kwargs).run(alert)


def action_client_for(execute, parser=None):
    """DryRunClient by default; the HTTP client only with --execute and ACTION_API_URL set."""
    if not execute:
        return DryRunClient()
    from oncall.action_api import ActionApiError, HttpActionClient
    try:
        return HttpActionClient()
    except ActionApiError as e:
        if parser:
            parser.error(str(e))
        raise


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("alert", help="alert JSON file")
    add_source_args(p)
    p.add_argument("--max-steps", type=int)
    p.add_argument("--model")
    p.add_argument("--auto-approve", "--yes", "-y", action="store_true",
                   help="approve every step automatically: DRY RUN ONLY, never executes")
    p.add_argument("--execute", action="store_true",
                   help="send approved steps to the Action API (ACTION_API_URL); default is a dry run")
    p.add_argument("--non-interactive", action="store_true",
                   help="leave approval pending without prompting interactive human confirmation")
    args = p.parse_args()
    apply_source_args(p, args)
    alert = json.loads(Path(args.alert).read_text())
    interactive = False if (args.auto_approve or args.non_interactive) else None
    record = run_agent(alert, model=args.model, max_steps=args.max_steps,
                       auto_approve=args.auto_approve, interactive=interactive,
                       action_client=action_client_for(args.execute, p))
    raise SystemExit(0 if record["investigation"]["outcome"] == "submitted" else 1)


if __name__ == "__main__":
    main()
