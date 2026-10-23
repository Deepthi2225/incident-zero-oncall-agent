"""Central configuration. Values can be overridden with environment variables (see .env.example).

Only endpoints and generic facts about the demo live here. No incident-specific answers.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path):
    """Read KEY=VALUE lines from a local .env (gitignored). Real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip().removeprefix("export ").strip(), value.strip().strip("'\"")
        if key and value and key not in os.environ:
            os.environ[key] = value


load_dotenv(ROOT / ".env")

RUNS_DIR = Path(os.getenv("ONCALL_RUNS_DIR", ROOT / "runs"))
BASELINE_FILE = Path(os.getenv("ONCALL_BASELINE", RUNS_DIR / "baseline.json"))
ACTION_LOG = Path(os.getenv("ONCALL_ACTION_LOG", RUNS_DIR / "actions.jsonl"))

# --- data source -------------------------------------------------------------
# "live" talks to the running demo (default); "bundle" replays a directory recorded from a live run.
# The "test" source (fixtures under tests/fixtures/) is never selected from the environment: it
# needs an explicit --test flag.
SOURCE = os.getenv("ONCALL_SOURCE", "live")
FIXTURE_DIR = Path(os.getenv("ONCALL_FIXTURE_DIR", ROOT / "tests" / "fixtures" / "payment_down"))
BUNDLE_DIR = Path(os.getenv("ONCALL_BUNDLE_DIR", ROOT / "bundles" / "current"))

# --- endpoints ---------------------------------------------------------------
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
# Tried in order until one answers /services. The 16686 port is published randomly by compose,
# so set JAEGER_URL explicitly if the proxy path does not work.
# Verified live: Jaeger 2.21 serves only API v3 under base_path /jaeger/ui. A base ending in /v3
# uses the v3 adapter (sources/jaeger_v3.py); others use the legacy v1 API.
JAEGER_CANDIDATES = [u for u in [
    os.getenv("JAEGER_URL"),
    "http://localhost:8080/jaeger/ui/api/v3",
    "http://localhost:8080/jaeger/ui/api",
    "http://localhost:16686/jaeger/ui/api/v3",
] if u]
OPENSEARCH_URL = os.getenv("OPENSEARCH_URL", "")  # optional; docker logs is the baseline
ACTION_API_URL = os.getenv("ACTION_API_URL", "")  # Day 2; unknown until organisers publish it
DEMO_DIR = os.getenv("OTEL_DEMO_DIR", "")         # folder with compose.yaml, for `docker compose`
# Limit container listing to the demo's compose project (verified label: opentelemetry-demo), so
# other projects' stopped containers on this laptop are not reported. Set to "" to list all.
COMPOSE_PROJECT = os.getenv("COMPOSE_PROJECT", "opentelemetry-demo")
HTTP_TIMEOUT = float(os.getenv("ONCALL_HTTP_TIMEOUT", "10"))

# HARD RULE 2: never read feature-flag state. Every outbound URL and file path is checked
# against these substrings by sources/guard.py.
FORBIDDEN_SUBSTRINGS = [
    "/feature", "/flagservice", ":8013", ":8016", "flagd", "demo.flagd.json",
]

# --- metrics -----------------------------------------------------------------
# Span-metric names differ between demo versions. sources discover the real names at startup
# by intersecting these candidates with /api/v1/label/__name__/values.
CALLS_METRIC_CANDIDATES = [
    "traces_span_metrics_calls_total", "calls_total", "traces_spanmetrics_calls_total",
]
DURATION_METRIC_CANDIDATES = [
    "traces_span_metrics_duration_milliseconds_bucket", "duration_milliseconds_bucket",
    "traces_spanmetrics_latency_bucket", "latency_bucket",
]
SERVICE_LABEL = os.getenv("ONCALL_SERVICE_LABEL", "service_name")
STATUS_LABEL = os.getenv("ONCALL_STATUS_LABEL", "status_code")
ERROR_STATUS = os.getenv("ONCALL_ERROR_STATUS", "STATUS_CODE_ERROR")
SPAN_KIND_LABEL = "span_kind"
SERVER_KINDS = ["SPAN_KIND_SERVER", "SPAN_KIND_CONSUMER"]

# --- alert thresholds (watcher, no LLM) --------------------------------------
ALERT_ERROR_RATE = float(os.getenv("ALERT_ERROR_RATE", "0.05"))      # 5 % of calls
ALERT_P95_MS = float(os.getenv("ALERT_P95_MS", "1500"))
ALERT_P95_X_BASELINE = float(os.getenv("ALERT_P95_X_BASELINE", "3"))  # 3x baseline p95
ALERT_MIN_RPS = float(os.getenv("ALERT_MIN_RPS", "0.05"))             # ignore near-idle series

# --- agent -------------------------------------------------------------------
MODEL = os.getenv("ONCALL_MODEL", "claude-opus-5-5")
EFFORT = os.getenv("ONCALL_EFFORT", "high")
MAX_STEPS = int(os.getenv("ONCALL_MAX_STEPS", "10"))
# Server-side refusal fallback. Disable if the organisers' key/proxy rejects the beta.
USE_FALLBACKS = os.getenv("ONCALL_FALLBACKS", "1") == "1"
TOOL_RESULT_MAX_BYTES = 2000
# Kill switch: create this file (python -m oncall stop) and the agent halts before its next
# model call or tool call. Delete it to allow runs again.
KILL_SWITCH_FILE = Path(os.getenv("ONCALL_KILL_SWITCH", ROOT / "STOP"))

# --- demo topology (from the demo's compose depends_on; generic, not incident answers) ---
DEPENDENCIES = {
    "load-generator": ["frontend-proxy"],
    "frontend-proxy": ["frontend", "image-provider"],
    "frontend": ["ad", "cart", "checkout", "currency", "product-catalog", "quote",
                 "recommendation", "shipping", "image-provider"],
    "checkout": ["cart", "currency", "email", "payment", "product-catalog", "shipping", "kafka"],
    "cart": ["valkey-cart"],
    "product-catalog": ["astronomy-db"],
    "recommendation": ["product-catalog"],
    "shipping": ["quote"],
    "kafka": ["accounting", "fraud-detection"],
}

CRITICALITY = {
    "frontend-proxy": "critical", "frontend": "critical", "checkout": "critical", "payment": "critical",
    "cart": "high", "currency": "high", "product-catalog": "high", "shipping": "high",
    "ad": "medium", "email": "medium", "recommendation": "medium",
    "quote": "low", "image-provider": "low", "load-generator": "low",
}

# Containers that are observability plumbing, not the shop. Still reported, ranked last.
INFRA_CONTAINERS = {"otel-collector", "jaeger", "prometheus", "opensearch", "grafana",
                    "opamp-server", "flagd", "flagd-ui"}
