"""Action API settings. Environment variables (or the repo's gitignored .env) override the defaults.

This service is separate from the agent (oncall/): it never imports it, and the agent reaches it
only over HTTP.
"""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _value(raw):
    """Same rule as oncall.config.dotenv_value: quotes removed, an inline " # comment" dropped."""
    raw = raw.strip()
    if raw[:1] in ("'", '"') and raw[0] in raw[1:]:
        return raw[1:raw.index(raw[0], 1)]
    return re.split(r"\s+#", raw, maxsplit=1)[0].strip()


def _load_dotenv(path):
    """KEY=VALUE lines from the local .env; real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip().removeprefix("export ").strip(), _value(value)
        if key and value and key not in os.environ:
            os.environ[key] = value


_load_dotenv(ROOT / ".env")

HOST = "127.0.0.1"  # localhost only, never configurable to a public interface
PORT = int(os.getenv("ACTION_API_PORT", "8099"))
TOKEN = os.getenv("ACTION_API_TOKEN", "")
COOLDOWN_S = float(os.getenv("ACTION_API_COOLDOWN_S", "60"))
IDEMPOTENCY_TTL_S = float(os.getenv("ACTION_API_IDEMPOTENCY_TTL_S", "3600"))
AUDIT_LOG = Path(os.getenv("ACTION_API_LOG", ROOT / "runs" / "action_api.jsonl"))
# throttle to undo, cooldowns and idempotency results; survives an API restart
STATE_FILE = Path(os.getenv("ACTION_API_STATE", ROOT / "runs" / "action_api_state.json"))
COMPOSE_PROJECT = os.getenv("COMPOSE_PROJECT", "opentelemetry-demo")
# The same STOP file as the agent's kill switch (python -m oncall stop), read without importing
# oncall. While it exists every action request is refused with 423. (Based on Padma's checkpoint3
# branch, where the Action API checks the kill switch itself.)
KILL_SWITCH_FILE = Path(os.getenv("ONCALL_KILL_SWITCH", ROOT / "STOP"))
DOCKER_TIMEOUT_S = float(os.getenv("ACTION_API_DOCKER_TIMEOUT_S", "60"))

# The demo's APPLICATION services (container name == service name), including the cache and the
# database the shop depends on. Only these can be restarted.
RESTART_ALLOWLIST = {
    "frontend-proxy", "frontend", "ad", "cart", "checkout", "currency", "email", "payment",
    "product-catalog", "quote", "recommendation", "shipping", "image-provider",
    "accounting", "fraud-detection", "kafka", "valkey-cart", "astronomy-db",
}
# Never restarted, even if someone adds them to the allowlist: the observability stack (our eyes),
# the feature-flag services (HARD RULE 2), the load generator (throttle it instead) and our own
# fault injector.
RESTART_DENYLIST = {
    "prometheus", "jaeger", "opensearch", "grafana", "otel-collector", "opamp-server",
    "flagd", "flagd-ui", "load-generator",
}
DENY_SUBSTRINGS = ("faultctl", "flagd", "feature")

# Locust's own web/control API (the demo publishes it through the proxy at /loadgen/).
LOCUST_URL = os.getenv("LOCUST_URL", "http://localhost:8080/loadgen").rstrip("/")
LOCUST_MAX_USERS = int(os.getenv("LOCUST_MAX_USERS", "50"))
HTTP_TIMEOUT_S = float(os.getenv("ACTION_API_HTTP_TIMEOUT_S", "10"))
# Outgoing URLs must never touch feature-flag state (HARD RULE 2).
FORBIDDEN_URL_SUBSTRINGS = ("/feature", "/flagservice", ":8013", ":8016", "flagd")
