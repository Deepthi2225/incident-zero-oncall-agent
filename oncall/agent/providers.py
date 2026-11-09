"""LLM providers behind one small interface, so the agent loop stays provider-independent.

    provider.create(system, tools, messages) -> response with
        .content      blocks: type "text" (.text), "tool_use" (.id, .name, .input), "thinking"
        .stop_reason  "tool_use" | "end_turn" | "refusal" | ...
        .usage        .input_tokens, .output_tokens

`messages` is the agent's single conversation in Anthropic Messages format. Providers translate it
on every call and keep no history of their own. Failures raise LLMError, which the loop records as
outcome "llm_error".

    AnthropicProvider  the agent's original Claude call, unchanged (thinking, effort, refusal fallback)
    GeminiProvider     Gemini through its OpenAI-compatible endpoint. Rebuilt from Shivakumar's Day 1
                       Gemini adapter: his reply-to-blocks conversion, without the second history.
    GroqProvider       Open models on Groq, through the same OpenAI-compatible code.
    OllamaProvider     A local model through Ollama, through the same code.
    FallbackProvider   Wraps a primary and uses a fallback when the primary fails.
"""
import json
import re
import time
from types import SimpleNamespace

import anthropic

from oncall import config

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(RuntimeError):
    """The model could not be called (API error, no credentials, missing package)."""


class NoCredentials(LLMError):
    pass


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, client, model, log=print, use_fallbacks=True):
        self.client, self.model, self.log, self.use_fallbacks = client, model, log, use_fallbacks

    def create(self, system, tools, messages):
        try:
            return self._create(system, tools, messages)
        except TypeError as e:  # the SDK raises TypeError when no API key or token is configured
            if "authentication" not in str(e):
                raise
            raise NoCredentials("no LLM credentials: set ANTHROPIC_API_KEY") from e
        except anthropic.APIError as e:
            raise LLMError(f"{type(e).__name__}: {e}") from e

    def _create(self, system, tools, messages):
        kwargs = dict(
            model=self.model, max_tokens=16000, system=system, tools=tools,
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


# --- Gemini (OpenAI-compatible endpoint) -------------------------------------------------------------

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_TOOL_RETRIES = 2
GROQ_TOOL_ERRORS = ("tool call validation failed", "tool_use_failed", "failed to parse tool call")
GROQ_RATE_WAITS = 6          # waits allowed for one model call
GROQ_RATE_WAIT_MAX_S = 65    # a longer wait means a daily limit, which is not worth waiting out
GROQ_RETRY_AFTER = re.compile(r"try again in (?:(\d+)h)?(?:(\d+)m(?!s))?(\d+(?:\.\d+)?)(ms|s)")


def groq_rate_limit_wait(text):
    """Seconds Groq asks us to wait, or None if this is not a rate-limit error that says so."""
    match = GROQ_RETRY_AFTER.search(text) if "rate limit" in text.lower() else None
    if not match:
        return None
    hours, minutes, amount, unit = match.groups()
    return int(hours or 0) * 3600 + int(minutes or 0) * 60 + float(amount) / (1000 if unit == "ms" else 1)
# JSON Schema keywords Gemini's function declarations do not accept; stripped for Gemini only.
GEMINI_UNSUPPORTED_KEYWORDS = {"additionalProperties", "strict", "$schema"}


def _get(block, key, default=None):
    return block.get(key, default) if isinstance(block, dict) else getattr(block, key, default)


def gemini_schema(schema):
    """A tool's input schema without the keywords Gemini rejects (recursively)."""
    if isinstance(schema, dict):
        return {k: gemini_schema(v) for k, v in schema.items() if k not in GEMINI_UNSUPPORTED_KEYWORDS}
    if isinstance(schema, list):
        return [gemini_schema(v) for v in schema]
    return schema


def to_openai_tools(tools):
    return [{"type": "function",
             "function": {"name": t["name"], "description": t.get("description", ""),
                          "parameters": gemini_schema(t.get("input_schema", {"type": "object"}))}}
            for t in tools]


def to_openai_messages(system, messages):
    """Our Anthropic-format conversation -> OpenAI chat messages, rebuilt on every call."""
    out = [{"role": "system", "content": system}]
    for m in messages:
        content = m["content"]
        if m["role"] == "user":
            if isinstance(content, str):
                out.append({"role": "user", "content": content})
                continue
            texts = []
            for part in content:
                if _get(part, "type") == "tool_result":
                    out.append({"role": "tool", "tool_call_id": _get(part, "tool_use_id"),
                                "content": _get(part, "content") or ""})
                elif _get(part, "type") == "text":
                    texts.append(_get(part, "text"))
            if texts:  # e.g. the step-budget note after the tool results
                out.append({"role": "user", "content": "\n".join(texts)})
        elif m["role"] == "assistant":
            blocks = [content] if isinstance(content, str) else content
            text = "\n".join(b if isinstance(b, str) else _get(b, "text", "")
                             for b in blocks if isinstance(b, str) or _get(b, "type") == "text")
            calls = []
            for b in blocks:
                if not isinstance(b, str) and _get(b, "type") == "tool_use":
                    call = {"id": _get(b, "id"), "type": "function",
                            "function": {"name": _get(b, "name"), "arguments": json.dumps(_get(b, "input") or {})}}
                    if _get(b, "extra_content"):  # Gemini's thought signature must be sent back
                        call["extra_content"] = _get(b, "extra_content")
                    calls.append(call)
            msg = {"role": "assistant", "content": text or ""}  # Ollama rejects a null content
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
    return out


def from_openai_reply(res):
    """OpenAI chat completion -> our response shape (Shivakumar's Day 1 conversion)."""
    choice = res.choices[0]
    msg = choice.message
    blocks = []
    if msg.content:
        blocks.append(SimpleNamespace(type="text", text=msg.content))
    for tc in msg.tool_calls or []:
        try:
            inp = json.loads(tc.function.arguments or "{}")
        except ValueError:
            inp = {}
        extra = getattr(tc, "extra_content", None) or (getattr(tc, "model_extra", None) or {}).get("extra_content")
        blocks.append(SimpleNamespace(type="tool_use", id=tc.id, name=tc.function.name, input=inp,
                                      extra_content=extra))
    finish = getattr(choice, "finish_reason", None)
    stop = "tool_use" if msg.tool_calls else "refusal" if finish == "content_filter" else "end_turn"
    usage = getattr(res, "usage", None)
    return SimpleNamespace(content=blocks, stop_reason=stop, usage=SimpleNamespace(
        input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        output_tokens=getattr(usage, "completion_tokens", 0) or 0))


class OpenAICompatibleProvider:
    """Chat completions with tools on any OpenAI-compatible endpoint. Subclasses set the three names."""
    name = base_url = key_setting = None

    def __init__(self, model, api_key=None, client=None, errors=None):
        """client/errors: injected in tests. Otherwise the openai package is imported here only."""
        if client is None:
            try:
                import openai
            except ImportError as e:
                raise LLMError(f"ONCALL_PROVIDER={self.name} needs the openai package: pip install openai") from e
            if not api_key:
                raise NoCredentials(f"no LLM credentials: set {self.key_setting}")
            client = openai.OpenAI(api_key=api_key, base_url=self.base_url)
            errors = (openai.OpenAIError,)
        self.client, self.model = client, model
        self.errors = errors or ()

    def create(self, system, tools, messages):
        try:
            res = self.client.chat.completions.create(
                model=self.model, messages=to_openai_messages(system, messages), tools=to_openai_tools(tools))
        except self.errors as e:
            raise LLMError(f"{type(e).__name__}: {e}") from e
        return from_openai_reply(res)


class GeminiProvider(OpenAICompatibleProvider):
    name, base_url, key_setting = "gemini", GEMINI_BASE_URL, "GEMINI_API_KEY"


class OllamaProvider(OpenAICompatibleProvider):
    """A local model through Ollama's OpenAI-compatible endpoint. The model must support tools."""
    name, base_url, key_setting = "ollama", None, "OLLAMA_URL"

    def __init__(self, model, base_url=None, client=None, errors=None):
        self.base_url = base_url or config.OLLAMA_URL
        super().__init__(model, api_key="ollama", client=client, errors=errors)  # Ollama ignores the key


class FallbackProvider:
    """Tries the primary on every call and uses the fallback only when the primary raises LLMError.
    The conversation is provider-neutral, so either can answer any turn."""

    def __init__(self, primary, fallback, log=print):
        self.primary, self.fallback, self.log = primary, fallback, log
        self.name = primary.name
        self.fallback_calls = 0

    def create(self, system, tools, messages):
        try:
            return self.primary.create(system, tools, messages)
        except LLMError as first:
            self.log(f"!!! {self.primary.name} failed ({str(first)[:160]}); "
                     f"using fallback {self.fallback.name} ({self.fallback.model})")
            try:
                response = self.fallback.create(system, tools, messages)
            except LLMError as second:
                raise LLMError(f"{self.primary.name}: {first}; fallback {self.fallback.name}: {second}") from second
            self.fallback_calls += 1
            return response


class GroqProvider(OpenAICompatibleProvider):
    """Groq validates tool arguments on its servers and answers 400 when they do not match the schema.
    The model gets the error and another try; the agent's conversation is not changed.
    Per-minute rate limits are waited out for the time Groq asks; longer limits stay errors."""
    name, base_url, key_setting = "groq", GROQ_BASE_URL, "GROQ_API_KEY"
    sleep = staticmethod(time.sleep)

    def create(self, system, tools, messages):
        attempt_messages = messages
        tool_retries = rate_waits = 0
        while True:
            try:
                return super().create(system, tools, attempt_messages)
            except LLMError as e:
                text = str(e)
                wait = groq_rate_limit_wait(text)
                if wait is not None and wait <= GROQ_RATE_WAIT_MAX_S and rate_waits < GROQ_RATE_WAITS:
                    rate_waits += 1
                    print(f"  (Groq rate limit: waiting {wait + 1:.0f} s)", flush=True)
                    self.sleep(wait + 1)
                elif tool_retries < GROQ_TOOL_RETRIES and any(m in text.lower() for m in GROQ_TOOL_ERRORS):
                    tool_retries += 1
                    attempt_messages = messages + [{"role": "user", "content": (
                        f"Your last tool call was rejected: {text[:500]}\n"
                        "Call the tool again with arguments that match its schema exactly. "
                        "Fields of type array must be JSON arrays, even with one item.")}]
                else:
                    raise
