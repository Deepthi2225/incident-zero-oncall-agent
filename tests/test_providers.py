"""LLM providers: Gemini as the optional second provider (based on Shivakumar's Day 1 Gemini adapter),
tested with a fake OpenAI client; the agent loop, validator and incident record stay unchanged."""
import json
import sys
from types import SimpleNamespace

import pytest

from oncall import config
from oncall.agent import run
from oncall.agent.providers import (AnthropicProvider, GeminiProvider, GroqProvider, LLMError, NoCredentials,
                                    gemini_schema, groq_rate_limit_wait, to_openai_messages)


class FakeAPIError(Exception):
    pass


def tool_call(cid, name, args, extra=None):
    return SimpleNamespace(id=cid, type="function", extra_content=extra,
                           function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def reply(content=None, calls=None, prompt=100, completion=20, finish="stop"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=calls),
                                 finish_reason=finish)],
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion))


class FakeOpenAI:
    """client.chat.completions.create(**kwargs) -> scripted replies; records every request."""

    def __init__(self, script):
        self.script, self.requests = script, []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs, default=str)))
        out = self.script(len(self.requests), kwargs)
        if isinstance(out, Exception):
            raise out
        return out


def gemini_agent(monkeypatch, script):
    fake = FakeOpenAI(script)
    monkeypatch.setattr(run, "GeminiProvider",
                        lambda model, key: GeminiProvider(model, client=fake, errors=(FakeAPIError,)))
    agent = run.Agent(provider="gemini", verbose=False)
    return agent, fake


def last_tool_result(kwargs):
    return json.loads(next(m for m in reversed(kwargs["messages"]) if m["role"] == "tool")["content"])


def diagnose_payment(n, kwargs):
    if n == 1:
        return reply(calls=[tool_call("call_1", "get_deploy_history", {"lookback": "30m"},
                                      extra={"google": {"thought_signature": "sig-1"}})], prompt=900, completion=40)
    eid = last_tool_result(kwargs)["changed_or_unhealthy"][0]["evidence"]
    return reply(calls=[tool_call("call_2", "submit_diagnosis", {
        "headline": "payment is down", "affected_services": [], "anomalies": [],
        "root_causes": [{"component": "payment", "fault_class": "container_stopped_or_restarting",
                         "cause": "payment exited", "confidence": 0.9, "evidence": [eid]}],
        "proposed_action": {"action": "restart_service", "target": "payment", "reason": "restart it"},
        "prevention": [], "open_questions": []})], prompt=1500, completion=80)


def test_tool_call_round_trip_and_submit_diagnosis(payment_down, monkeypatch):
    agent, fake = gemini_agent(monkeypatch, diagnose_payment)
    rec = agent.run({"incident_id": "INC-G", "service": "checkout"})

    first, second = fake.requests
    assert first["messages"][0] == {"role": "system", "content": agent.system}
    assert first["model"] == config.GEMINI_MODEL
    # round trip: our tool_use comes back as tool_calls (with Gemini's thought signature), the
    # result as a role "tool" message with the same id; no second history anywhere
    assistant = second["messages"][2]
    assert assistant["tool_calls"][0]["id"] == "call_1"
    assert assistant["tool_calls"][0]["extra_content"] == {"google": {"thought_signature": "sig-1"}}
    tool = second["messages"][3]
    assert tool["role"] == "tool" and tool["tool_call_id"] == "call_1" and "changed_or_unhealthy" in tool["content"]

    d = rec["diagnosis"]  # parsed and validated by the same validator as for Claude
    assert d["status"] == "diagnosed" and d["root_causes"][0]["component"] == "payment"
    assert rec["remediation_plan"]["steps"][0]["target"] == "payment"
    assert (rec["metadata"]["provider"], rec["metadata"]["model"]) == ("gemini", config.GEMINI_MODEL)
    acc = rec["accounting"]
    assert (acc["tokens_in"], acc["tokens_out"], acc["steps_used"]) == (2400, 120, 2)
    assert acc["estimated_cost_usd"] is None  # no Gemini price table


def test_schemas_sent_to_gemini_have_no_rejected_keywords(payment_down, monkeypatch):
    agent, fake = gemini_agent(monkeypatch, diagnose_payment)
    agent.run({"service": "checkout"})
    text = json.dumps(fake.requests[0]["tools"])
    assert "additionalProperties" not in text and '"strict"' not in text
    submit = next(t for t in fake.requests[0]["tools"] if t["function"]["name"] == "submit_diagnosis")
    rc = submit["function"]["parameters"]["properties"]["root_causes"]["items"]["properties"]
    assert "container_stopped_or_restarting" in rc["fault_class"]["enum"]  # enums are kept
    assert "additionalProperties" in json.dumps(run.SUBMIT_TOOL)  # Claude's schema is untouched


def test_reply_without_a_tool_call_gets_a_nudge(payment_down, monkeypatch):
    def script(n, kwargs):
        if n == 1:
            return reply(content="Let me think about the alert first.")
        return diagnose_payment(n - 1, kwargs)

    agent, fake = gemini_agent(monkeypatch, script)
    rec = agent.run({"service": "checkout"})
    nudge = fake.requests[1]["messages"]
    assert nudge[-2] == {"role": "assistant", "content": "Let me think about the alert first."}
    assert nudge[-1] == {"role": "user", "content": "Use the tools to continue, or call submit_diagnosis."}
    assert rec["investigation"]["outcome"] == "submitted"


def test_api_failure_is_llm_error(payment_down, monkeypatch):
    agent, _ = gemini_agent(monkeypatch, lambda n, kw: FakeAPIError("503 model overloaded"))
    rec = agent.run({"service": "checkout"})
    assert rec["investigation"]["outcome"] == "llm_error"
    assert "FakeAPIError: 503 model overloaded" in rec["investigation"]["error"]
    assert rec["status"] == "complete" and rec["outcome"]["status"] == "escalated"
    with pytest.raises(LLMError):
        GeminiProvider("m", client=FakeOpenAI(lambda n, kw: FakeAPIError("x")),
                       errors=(FakeAPIError,)).create("s", [], [{"role": "user", "content": "hi"}])


def test_step_budget_note_and_text_follow_the_tool_results():
    messages = [
        {"role": "user", "content": "ALERT"},
        {"role": "assistant", "content": [SimpleNamespace(type="thinking", thinking="hmm"),
                                          SimpleNamespace(type="text", text="checking"),
                                          SimpleNamespace(type="tool_use", id="c1", name="t", input={"a": 1})]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "{}"},
                                     {"type": "text", "text": "Step budget exhausted."}]},
    ]
    out = to_openai_messages("SYS", messages)
    assert out[2] == {"role": "assistant", "content": "checking", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "t", "arguments": '{"a": 1}'}}]}
    assert out[3] == {"role": "tool", "tool_call_id": "c1", "content": "{}"}
    assert out[4] == {"role": "user", "content": "Step budget exhausted."}


def test_gemini_schema_strips_only_rejected_keywords():
    schema = {"type": "object", "additionalProperties": False, "properties": {
        "x": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                         "properties": {"k": {"type": "string", "enum": ["a", "b"]}}}}}}
    assert gemini_schema(schema) == {"type": "object", "properties": {"x": {"type": "array", "items": {
        "type": "object", "properties": {"k": {"type": "string", "enum": ["a", "b"]}}}}}}


def test_provider_is_chosen_explicitly_never_from_the_keys(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "gemini-key-set")
    monkeypatch.setattr(config, "PROVIDER", "anthropic")
    monkeypatch.setattr(run.anthropic, "Anthropic", lambda: SimpleNamespace())
    assert run.Agent(verbose=False).llm.name == "anthropic"  # a Gemini key alone changes nothing
    with pytest.raises(ValueError, match="unknown ONCALL_PROVIDER"):
        run.Agent(provider="gpt", verbose=False)


def test_gemini_needs_the_openai_package_and_a_key(monkeypatch):
    monkeypatch.setitem(sys.modules, "openai", None)  # import fails as if not installed
    with pytest.raises(LLMError, match="openai package"):
        GeminiProvider("m", api_key="k")
    fake_openai = SimpleNamespace(OpenAI=lambda **kw: None, OpenAIError=FakeAPIError)
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    with pytest.raises(NoCredentials, match="GEMINI_API_KEY"):
        GeminiProvider("m", api_key="")


def test_groq_runs_the_same_loop_and_is_recorded(payment_down, monkeypatch):
    fake = FakeOpenAI(diagnose_payment)
    monkeypatch.setattr(run, "GroqProvider", lambda model, key: GroqProvider(model, client=fake, errors=(FakeAPIError,)))
    agent = run.Agent(provider="groq", verbose=False)
    rec = agent.run({"incident_id": "INC-Q", "service": "checkout"})
    assert fake.requests[0]["model"] == config.GROQ_MODEL
    assert "additionalProperties" not in json.dumps(fake.requests[0]["tools"])
    assert rec["diagnosis"]["root_causes"][0]["component"] == "payment"
    assert (rec["metadata"]["provider"], rec["metadata"]["model"]) == ("groq", config.GROQ_MODEL)
    assert rec["accounting"]["estimated_cost_usd"] is None  # no Groq price table


def test_groq_uses_its_own_endpoint_and_key(monkeypatch):
    seen = {}
    fake_openai = SimpleNamespace(OpenAI=lambda **kw: seen.update(kw), OpenAIError=FakeAPIError)
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    GroqProvider("m", api_key="gsk-test")
    assert seen == {"api_key": "gsk-test", "base_url": "https://api.groq.com/openai/v1"}
    with pytest.raises(NoCredentials, match="GROQ_API_KEY"):
        GroqProvider("m", api_key="")
    monkeypatch.setitem(sys.modules, "openai", None)
    with pytest.raises(LLMError, match="ONCALL_PROVIDER=groq needs the openai package"):
        GroqProvider("m", api_key="k")


REJECTED = "Error code: 400 - Tool call validation failed: /prevention: expected array, but got string"


def test_groq_rejected_tool_call_is_retried_with_the_error(payment_down, monkeypatch):
    def script(n, kwargs):
        if n == 2:  # the submit_diagnosis attempt, as in the real failure
            return FakeAPIError(REJECTED)
        return diagnose_payment(1 if n == 1 else 2, kwargs)

    fake = FakeOpenAI(script)
    monkeypatch.setattr(run, "GroqProvider", lambda model, key: GroqProvider(model, client=fake, errors=(FakeAPIError,)))
    rec = run.Agent(provider="groq", verbose=False).run({"service": "checkout"})
    assert len(fake.requests) == 3
    assert "expected array" in fake.requests[2]["messages"][-1]["content"]
    assert rec["investigation"]["outcome"] == "submitted"
    assert rec["diagnosis"]["root_causes"][0]["component"] == "payment"


def groq_with(script):
    fake = FakeOpenAI(script)
    return GroqProvider("m", client=fake, errors=(FakeAPIError,)), fake


def test_groq_gives_up_after_the_retries_and_keeps_the_conversation():
    provider, fake = groq_with(lambda n, kw: FakeAPIError(REJECTED))
    messages = [{"role": "user", "content": "hi"}]
    with pytest.raises(LLMError, match="validation failed"):
        provider.create("s", [], messages)
    assert len(fake.requests) == 3
    assert messages == [{"role": "user", "content": "hi"}]


def test_groq_does_not_retry_other_errors():
    provider, fake = groq_with(lambda n, kw: FakeAPIError("503 overloaded"))
    with pytest.raises(LLMError, match="503"):
        provider.create("s", [], [{"role": "user", "content": "hi"}])
    assert len(fake.requests) == 1


LIMITED = "RateLimitError: 429 - Rate limit reached on tokens per minute (TPM): Please try again in 2.04s. Need more?"


def test_groq_waits_out_a_per_minute_limit():
    provider, fake = groq_with(lambda n, kw: FakeAPIError(LIMITED) if n == 1 else reply(content="ok"))
    waits = []
    provider.sleep = waits.append
    out = provider.create("s", [], [{"role": "user", "content": "hi"}])
    assert out.content[0].text == "ok" and len(fake.requests) == 2
    assert waits == [pytest.approx(3.04)]


def test_groq_does_not_wait_out_a_daily_limit():
    daily = LIMITED.replace("2.04s", "1h12m3.5s")
    provider, fake = groq_with(lambda n, kw: FakeAPIError(daily))
    provider.sleep = lambda s: pytest.fail("must not wait")
    with pytest.raises(LLMError, match="Rate limit"):
        provider.create("s", [], [{"role": "user", "content": "hi"}])
    assert len(fake.requests) == 1


def test_groq_stops_waiting_after_the_limit_of_waits():
    provider, fake = groq_with(lambda n, kw: FakeAPIError(LIMITED))
    provider.sleep = lambda s: None
    with pytest.raises(LLMError):
        provider.create("s", [], [{"role": "user", "content": "hi"}])
    assert len(fake.requests) == 7


@pytest.mark.parametrize("text,seconds", [("2.04s", 2.04), ("450ms", 0.45), ("1m30.5s", 90.5)])
def test_groq_wait_parsing(text, seconds):
    assert groq_rate_limit_wait(f"rate limit: try again in {text}.") == pytest.approx(seconds)


def test_groq_key_is_a_known_secret(monkeypatch):
    from oncall import record
    monkeypatch.setattr(config, "GROQ_API_KEY", "gsk-not-a-real-key")
    assert "gsk-not-a-real-key" in record._known_secrets()


def test_anthropic_missing_credentials_is_llm_error():
    def no_key(**kwargs):
        raise TypeError("Could not resolve authentication method")
    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=no_key)))
    with pytest.raises(NoCredentials, match="ANTHROPIC_API_KEY"):
        AnthropicProvider(client, "claude-opus-5-5", use_fallbacks=False).create("s", [], [])
