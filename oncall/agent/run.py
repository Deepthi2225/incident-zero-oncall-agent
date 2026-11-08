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
from oncall.agent.actions import hold_for_human
from oncall.agent.diagnosis import CONFIDENT, SUBMIT_TOOL, empty_summary, validate
from oncall.agent.reviewer import review_diagnosis
from oncall.agent.providers import (AnthropicProvider, FallbackProvider, GeminiProvider, GroqProvider, LLMError,
                                    NoCredentials, OllamaProvider)
from oncall.action_api import DryRunClient
from oncall.record import RunRecord, unique_run_id
from oncall.remediate import make_approver, run_plan
from oncall.report import render_diagnosis, render_outcome
from oncall.evidence import registry
from oncall.sources import add_source_args, apply_source_args, source_info
from oncall.tools.catalog import TOOL_SPECS, dispatch

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
                 action_client=None, verifier=None, provider=None):
        self.max_steps = max_steps or config.MAX_STEPS
        self.verbose = verbose
        # The provider is chosen explicitly (ONCALL_PROVIDER), never from which keys happen to be set.
        name = (provider or config.PROVIDER).strip().lower()
        if name == "anthropic":
            self.client = anthropic.Anthropic()  # created here so tests can replace run.anthropic.Anthropic
            self.model = model or config.MODEL
            self.llm = AnthropicProvider(self.client, self.model, log=self.log,
                                         use_fallbacks=config.USE_FALLBACKS)
        elif name == "gemini":  # optional second provider (based on Shivakumar's Day 1 Gemini adapter)
            self.model = model or config.GEMINI_MODEL
            self.llm = GeminiProvider(self.model, config.GEMINI_API_KEY)
        elif name == "groq":
            self.model = model or config.GROQ_MODEL
            self.llm = GroqProvider(self.model, config.GROQ_API_KEY)
        elif name == "ollama":
            self.model = model or config.OLLAMA_MODEL
            self.llm = OllamaProvider(self.model)
        else:
            raise ValueError(f"unknown ONCALL_PROVIDER {name!r}; use anthropic, gemini, groq or ollama")
        fallback = config.FALLBACK_PROVIDER.strip().lower()
        if fallback not in ("", "ollama"):
            raise ValueError(f"unknown ONCALL_FALLBACK_PROVIDER {fallback!r}; use ollama or leave it empty")
        if fallback == "ollama" and name != "ollama":
            self.llm = FallbackProvider(self.llm, OllamaProvider(config.OLLAMA_MODEL), log=self.log)
        self.auto_approve = auto_approve
        self.interactive = (not auto_approve) if interactive is None else interactive
        # Dry run unless a live Action API client is passed in (--execute).
        self.action_client = action_client or DryRunClient()
        self._rec = None  # the run being recorded, for the "approval needed" page
        self.approve = make_approver(auto_approve, self.interactive, on_prompt=self._page)
        self.verifier = verifier
        self.tools = TOOL_SPECS + [SUBMIT_TOOL]
        self.system = build_system_prompt(self.max_steps)

    def log(self, text):
        if self.verbose:
            print(text, flush=True)

    def _page(self, step, diag):
        """Called by the approver just before it shows the approval box: page the on-call engineer
        in the background (oncall/notify.py page()); the prompt is not delayed."""
        if self._rec is not None:
            self._rec.page_approval(step, log=self.log)

    @property
    def use_fallbacks(self):  # Anthropic refusal fallbacks; kept on the agent for callers and tests
        return getattr(self.llm, "use_fallbacks", False)

    @use_fallbacks.setter
    def use_fallbacks(self, value):
        if hasattr(self.llm, "use_fallbacks"):
            self.llm.use_fallbacks = value

    def _call_model(self, messages):
        return self.llm.create(self.system, self.tools, messages)

    def run(self, alert):
        """One incident: investigate -> diagnose -> plan step by step. Returns the incident record
        (exactly what is saved in runs/<run_id>.json) plus its "path"."""
        registry.reset()
        if hasattr(self.llm, "fallback_calls"):  # counted per incident; `watch` reuses the agent
            self.llm.fallback_calls = 0
        info = source_info()
        run_id = unique_run_id(datetime.now().strftime("%Y%m%d-%H%M%S"), info["source"])
        rec = RunRecord(run_id, alert, info, model=self.model, max_steps=self.max_steps,
                        provider=self.llm.name)
        rec.save()  # phase: alert received
        self._rec = rec
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
        except LLMError as e:  # any provider: API failure, no credentials, missing package
            outcome, error = "llm_error", str(e)
            self.log(f"!!! LLM error: {error[:300]}")

        rec.investigation_done(outcome, error)
        # Counted before the reviewer's call: a review alone does not make it a fallback diagnosis.
        diagnosed_on_fallback = outcome == "submitted" and getattr(self.llm, "fallback_calls", 0) > 0
        if summary is None:
            summary = empty_summary(f"agent outcome: {outcome}")
        elif outcome == "submitted" and config.REVIEWER:
            # optional second opinion before anything is shown for approval; it can only lower
            # confidence and remove steps (stretch goal 2, oncall/agent/reviewer.py)
            review_diagnosis(summary, alert, self.llm, CONFIDENT, on_tokens=rec.review_tokens, log=self.log)
        if getattr(self.llm, "fallback_calls", 0):  # say so when another model produced part of this
            rec.data["metadata"]["fallback"] = {"provider": self.llm.fallback.name, "model": self.llm.fallback.model,
                                                "model_calls": self.llm.fallback_calls,
                                                "diagnosis_held": diagnosed_on_fallback}
        if diagnosed_on_fallback:  # a weaker local model never puts a fix in front of the on-call engineer
            hold_for_human(summary, f"the fallback model {self.llm.fallback.model} helped produce this diagnosis, "
                                    "so no fix is offered; a human must confirm it")
        rec.set_diagnosis(summary)  # phase: diagnosis
        self.log(render_diagnosis(rec.saved))

        if outcome == "submitted":  # one step at a time: approve -> act -> verify
            try:
                run_plan(summary, self.action_client, self.approve, self._verifier(), run_id, self.log,
                         on_update=rec.plan_updated)
            except Exception as e:  # never lose the diagnosis because the plan failed
                summary["remediation_plan"]["error"] = f"{type(e).__name__}: {e}"
                self.log(f"!!! remediation plan error: {e}")
        final = rec.finish(log=self.log)  # phase: outcome (report, escalation notifications, index line)
        self.log(render_outcome(final))
        self.log(f"=== saved {rec.path}")

    def _verifier(self):
        if self.verifier is None:
            from oncall.verify import Verifier
            self.verifier = Verifier()
        return self.verifier


def run_agent(alert, **kwargs):
    return Agent(**kwargs).run(alert)


EXECUTE_NEEDS_HUMAN = "--execute requires a human approval; --auto-approve only works with dry runs."


def check_execute_flags(parser, args):
    """Refuse --execute with --auto-approve/--yes at startup instead of refusing every step later.
    (Based on Aniruddha's aj_ck_3 branch.)"""
    if getattr(args, "execute", False) and getattr(args, "auto_approve", False):
        parser.error(EXECUTE_NEEDS_HUMAN)


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
    check_execute_flags(p, args)
    apply_source_args(p, args)
    alert = json.loads(Path(args.alert).read_text(encoding="utf-8"))
    interactive = False if (args.auto_approve or args.non_interactive) else None
    record = run_agent(alert, model=args.model, max_steps=args.max_steps,
                       auto_approve=args.auto_approve, interactive=interactive,
                       action_client=action_client_for(args.execute, p))
    raise SystemExit(0 if record["investigation"]["outcome"] == "submitted" else 1)


if __name__ == "__main__":
    main()
