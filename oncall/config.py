"""Central configuration. Values can be overridden with environment variables (see .env.example).

Only endpoints and generic facts about the demo live here. No incident-specific answers.
"""
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# A Windows console or pipe may not be able to encode every character; never crash on printing one.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")


def dotenv_value(raw):
    """The value of a .env line: surrounding quotes removed; for an unquoted value an inline
    " # comment" is dropped, so EMAIL_ENABLED=on  # comment reads as "on"."""
    raw = raw.strip()
    if raw[:1] in ("'", '"') and raw[0] in raw[1:]:
        return raw[1:raw.index(raw[0], 1)]
    return re.split(r"\s+#", raw, maxsplit=1)[0].strip()


def load_dotenv(path):
    """Read KEY=VALUE lines from a local .env (gitignored). Real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip().removeprefix("export ").strip(), dotenv_value(value)
        if key and value and key not in os.environ:
            os.environ[key] = value


_TRUE, _FALSE = ("on", "1", "true", "yes"), ("off", "0", "false", "no")


def env_flag(name, default):
    """An on/off setting: on/off, true/false, 1/0 or yes/no, any case. Anything else keeps the
    default and says so on stderr, instead of silently switching the feature off."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in _TRUE or value in _FALSE:
        return value in _TRUE
    print(f"warning: {name}={raw!r} is not on/off, true/false or 1/0; using {'on' if default else 'off'}",
          file=sys.stderr)
    return default


# ONCALL_DOTENV=off (set in the real environment) skips the repo's .env entirely: tests use it so
# child processes see only the settings the test gives them.
if env_flag("ONCALL_DOTENV", True):
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
# Logs: OpenSearch first (host port discovered with `docker port opensearch 9200` when this is
# empty), docker logs as the fallback. Verified index pattern: otel-logs-yyyy-MM-dd.
OPENSEARCH_URL = os.getenv("OPENSEARCH_URL", "")
OPENSEARCH_INDEX = os.getenv("OPENSEARCH_INDEX", "otel-logs-*")
ACTION_API_URL = os.getenv("ACTION_API_URL", "")  # Day 2; unknown until organisers publish it
ACTION_API_TOKEN = os.getenv("ACTION_API_TOKEN", "")  # shared with actionapi/; sent as a Bearer token
THROTTLE_USERS = int(os.getenv("ONCALL_THROTTLE_USERS", "2"))  # load-generator users when throttling
ACTION_API_TIMEOUT = float(os.getenv("ACTION_API_TIMEOUT", "30"))

# Verify after each action, against BASELINE_FILE. Metrics are judged from counter deltas since
# the action (a 3m rate() window would still contain the outage); each check needs a new sample,
# and samples arrive about every 60 s. VERIFY_WINDOW is only for the "before" snapshot.
VERIFY_WINDOW = os.getenv("VERIFY_WINDOW", "3m")
# Fast path: container running + successful server spans in Jaeger since the action. Reports
# "recovering" early and polls more often; "recovered" still needs the metrics checks.
VERIFY_FAST_PATH = os.getenv("VERIFY_FAST_PATH", "1") == "1"
VERIFY_FAST_INTERVAL_S = float(os.getenv("VERIFY_FAST_INTERVAL_S", "10"))
VERIFY_FAST_LOOKBACK_S = float(os.getenv("VERIFY_FAST_LOOKBACK_S", "30"))
# A metrics interval with fewer expected calls than this (baseline rps x interval) cannot show
# "no traffic" or a traffic drop; such a service is judged on errors and latency only.
VERIFY_MIN_EXPECTED_CALLS = float(os.getenv("VERIFY_MIN_EXPECTED_CALLS", "3"))
# Shop traffic at the entry points below this share of its baseline means nothing can be judged.
VERIFY_SHOP_MIN_RATIO = float(os.getenv("VERIFY_SHOP_MIN_RATIO", "0.1"))
VERIFY_TIMEOUT_S = float(os.getenv("VERIFY_TIMEOUT_S", "420"))
VERIFY_INTERVAL_S = float(os.getenv("VERIFY_INTERVAL_S", "30"))
VERIFY_CONSECUTIVE = int(os.getenv("VERIFY_CONSECUTIVE", "2"))      # healthy checks in a row
VERIFY_RPS_MIN_RATIO = float(os.getenv("VERIFY_RPS_MIN_RATIO", "0.5"))  # traffic back to >= 50%
VERIFY_RPS_MAX_RATIO = float(os.getenv("VERIFY_RPS_MAX_RATIO", "3.0"))  # and no flood left
VERIFY_ERR_MARGIN = float(os.getenv("VERIFY_ERR_MARGIN", "0.02"))      # errors <= baseline + 2 pts
VERIFY_P95_MAX_RATIO = float(os.getenv("VERIFY_P95_MAX_RATIO", "2.0"))  # p95 <= 2x baseline
VERIFY_P95_FLOOR_MS = float(os.getenv("VERIFY_P95_FLOOR_MS", "50"))    # ignore p95 below this
# toggle_feature_flag stays OFF until mentors confirm how a flag name may be known. Names may come
# only from allowed sources (e.g. the organisers' brief), never from flagd, flag files or the flag
# API (HARD RULE 2). ONCALL_FLAG_NAMES is a comma-separated list of those names.
ALLOW_FLAG_TOGGLE = os.getenv("ONCALL_ALLOW_FLAG_TOGGLE", "0") == "1"
ALLOWED_FLAG_NAMES = [f.strip() for f in os.getenv("ONCALL_FLAG_NAMES", "").split(",") if f.strip()]
DEMO_DIR = os.getenv("OTEL_DEMO_DIR", "")         # folder with compose.yaml, for `docker compose`
# Limit container listing to the demo's compose project (verified label: opentelemetry-demo), so
# other projects' stopped containers on this laptop are not reported. Set to "" to list all.
COMPOSE_PROJECT = os.getenv("COMPOSE_PROJECT", "opentelemetry-demo")
HTTP_TIMEOUT = float(os.getenv("ONCALL_HTTP_TIMEOUT", "10"))

# HARD RULE 2: never read feature-flag state. Every outbound URL and file path is checked
# against these substrings by sources/guard.py.
# "faultctl" is our own fault injector: the agent must never read or call it (it knows the answer).
FORBIDDEN_SUBSTRINGS = [
    "/feature", "/flagservice", ":8013", ":8016", "flagd", "demo.flagd.json", "faultctl",
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

# --- investigation thresholds (get_service_health) ---------------------------
# Alerting itself lives in detector.py (the one detector); these only decide which services
# the agent's health view flags. Same relative rules as the detector.
SLOW_NO_BASELINE_MS = float(os.getenv("SLOW_NO_BASELINE_MS", "1500"))  # used only without a baseline
SILENT_RATIO = float(os.getenv("SILENT_RATIO", "0.2"))                 # rps below 20% of baseline
SILENT_MIN_BASE_RPS = float(os.getenv("SILENT_MIN_BASE_RPS", "0.005"))  # idle services never silent

# --- agent -------------------------------------------------------------------
# LLM provider: chosen explicitly, never from which API keys happen to be set.
# gemini is the optional second provider (based on Shivakumar's Day 1 Gemini adapter).
PROVIDER = os.getenv("ONCALL_PROVIDER", "anthropic")
MODEL = os.getenv("ONCALL_MODEL", "claude-opus-5-5")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")  # .env only
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")  # .env only
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:8b")  # must support tool calling
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/v1")
FALLBACK_PROVIDER = os.getenv("ONCALL_FALLBACK_PROVIDER", "")  # ollama, or empty for no fallback
EFFORT = os.getenv("ONCALL_EFFORT", "high")
MAX_STEPS = int(os.getenv("ONCALL_MAX_STEPS", "10"))
UI_PORT = int(os.getenv("ONCALL_UI_PORT", "8090"))  # read-only live dashboard (python -m oncall.ui)
# Optional reviewer agent (stretch goal 2): one skeptical second opinion on the diagnosis before
# approval. It can only lower confidence. Off unless ONCALL_REVIEWER=on.
REVIEWER = env_flag("ONCALL_REVIEWER", False)
REVIEWER_TIMEOUT_S = float(os.getenv("ONCALL_REVIEWER_TIMEOUT_S", "30"))
# Email escalation (oncall/notify.py): off unless EMAIL_ENABLED=on. .env only; a real SMTP server
# sends incident data off this laptop. For a local fake server use SMTP_HOST=127.0.0.1, not
# localhost (on Windows localhost tries IPv6 ::1 first). runs/notifications.jsonl is always
# written. Check the settings with: python -m oncall emailtest
# EMAIL_TRANSPORT=resend sends through Resend's HTTPS API instead (RESEND_API_KEY). Our teammate's
# .env names are accepted too: NOTIFICATION_FROM_EMAIL (= EMAIL_FROM), ONCALL_PRIMARY_EMAIL (added
# to EMAIL_TO), SMTP_PASS (= SMTP_PASSWORD), ONCALL_PRIMARY_NAME (greeting).


def _env(*names):
    """The first of names that is set and not blank, whitespace stripped."""
    return next((v.strip() for v in (os.getenv(n) for n in names) if v and v.strip()), "")


EMAIL_ENABLED = env_flag("EMAIL_ENABLED", False)
EMAIL_TRANSPORT = _env("EMAIL_TRANSPORT").lower() or "smtp"
SMTP_HOST = _env("SMTP_HOST")
SMTP_PORT = int(_env("SMTP_PORT") or 587)
SMTP_USER = _env("SMTP_USER")
SMTP_PASSWORD = _env("SMTP_PASSWORD", "SMTP_PASS")
SMTP_STARTTLS = env_flag("SMTP_STARTTLS", True)
RESEND_API_KEY = _env("RESEND_API_KEY")
RESEND_URL = "https://api.resend.com/emails"
# Resend's shared test sender; it only delivers to the Resend account owner's own address
# until a domain is verified in Resend.
RESEND_DEFAULT_FROM = "AI On-Call Agent <onboarding@resend.dev>"
EMAIL_FROM = _env("EMAIL_FROM", "NOTIFICATION_FROM_EMAIL")
EMAIL_TO = list(dict.fromkeys(a.strip() for a in f"{_env('EMAIL_TO')},{_env('ONCALL_PRIMARY_EMAIL')}".split(",")
                              if a.strip()))
ONCALL_PRIMARY_NAME = _env("ONCALL_PRIMARY_NAME")
EMAIL_TIMEOUT_S = float(_env("EMAIL_TIMEOUT_S") or 10)
EMAIL_ON_RECOVERED = env_flag("EMAIL_ON_RECOVERED", False)
# "Approval needed" page: one email per plan step the moment its approval box is shown in the
# terminal. On by default whenever EMAIL_ENABLED=on; EMAIL_ON_APPROVAL=off turns just this off.
EMAIL_ON_APPROVAL = env_flag("EMAIL_ON_APPROVAL", True)
# Voice calls through Twilio: off unless CALLS_ENABLED=on. .env only.
CALLS_ENABLED = env_flag("CALLS_ENABLED", False)
TWILIO_ACCOUNT_SID = _env("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = _env("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = _env("TWILIO_FROM_NUMBER")
ONCALL_PRIMARY_PHONE = _env("ONCALL_PRIMARY_PHONE").replace(" ", "")
# Who is alerted for each severity (see escalation.example.json). No file: everyone in .env as before.
ESCALATION_FILE = Path(os.getenv("ONCALL_ESCALATION_FILE", ROOT / "escalation.json"))
# Telegram alerts through a bot: off unless TELEGRAM_ENABLED=on. .env only.
TELEGRAM_ENABLED = env_flag("TELEGRAM_ENABLED", False)
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID")
UI_URL = f"http://127.0.0.1:{UI_PORT}"  # where the read-only dashboard shows the waiting step
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

# Where shop traffic enters (the load generator calls frontend-proxy, which calls frontend).
ENTRY_SERVICES = ["frontend-proxy", "frontend"]

CRITICALITY = {
    "frontend-proxy": "critical", "frontend": "critical", "checkout": "critical", "payment": "critical",
    "cart": "high", "currency": "high", "product-catalog": "high", "shipping": "high",
    "ad": "medium", "email": "medium", "recommendation": "medium",
    "quote": "low", "image-provider": "low", "load-generator": "low",
}

# Containers that are observability plumbing, not the shop. Still reported, ranked last.
INFRA_CONTAINERS = {"otel-collector", "jaeger", "prometheus", "opensearch", "grafana",
                    "opamp-server", "flagd", "flagd-ui"}
