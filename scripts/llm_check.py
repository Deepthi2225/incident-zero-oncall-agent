"""One real tool-calling request to confirm the LLM key, model and tool use all work.

Usage: python scripts/llm_check.py
"""
import sys
from pathlib import Path

import anthropic

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from oncall import config  # noqa: E402

TOOL = {
    "name": "get_service_health",
    "description": "Return error rate and p95 latency per service.",
    "input_schema": {"type": "object", "properties": {"window": {"type": "string"}},
                     "required": ["window"], "additionalProperties": False},
}


def main():
    client = anthropic.Anthropic()
    try:
        response = client.messages.create(
            model=config.MODEL, max_tokens=2000, tools=[TOOL],
            output_config={"effort": "low"},
            messages=[{"role": "user", "content":
                       "Checkout alerts fired. Call get_service_health for the last 5 minutes."}],
        )
    except anthropic.AuthenticationError as e:
        sys.exit(f"FAIL auth: {e}")
    except anthropic.APIConnectionError as e:
        sys.exit(f"FAIL connection: {e}")
    except anthropic.APIStatusError as e:
        sys.exit(f"FAIL HTTP {e.status_code}: {e}")
    except TypeError as e:  # raised by the SDK when no credentials are configured at all
        sys.exit(f"FAIL no credentials: {e}")

    calls = [b for b in response.content if b.type == "tool_use"]
    if not calls:
        sys.exit(f"FAIL: no tool call (stop_reason={response.stop_reason})")
    print(f"OK model={response.model} tool={calls[0].name} input={calls[0].input} "
          f"tokens in/out={response.usage.input_tokens}/{response.usage.output_tokens}")


if __name__ == "__main__":
    main()
