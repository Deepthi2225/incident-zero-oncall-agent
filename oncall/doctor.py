"""`python -m oncall doctor`: is this machine ready, and if not, what to do next. Read-only.

Reports whether settings exist, never their values.
"""
import importlib.util
import os
import sys

import requests

from oncall import config
from oncall.sources import SourceError, live

OK, FIX, OPTIONAL = "ok", "fix", "optional"
SHOP_MINIMUM = 10  # containers; the core shop alone runs more than this


def _item(name, passed, detail, fix="", required=True):
    state = OK if passed else (FIX if required else OPTIONAL)
    return {"name": name, "state": state, "detail": detail if passed else "not ready", "fix": "" if passed else fix}


def _reaches(url):
    try:
        return requests.get(url, timeout=2).ok
    except requests.RequestException:
        return False


def _shop_containers():
    """How many shop containers are running, or None if Docker cannot be reached."""
    try:
        found, _ = live._docker("ps", "-q", "--filter", f"label=com.docker.compose.project={config.COMPOSE_PROJECT}")
    except SourceError:
        return None
    return len(found.split())


def _model():
    provider = config.PROVIDER.strip().lower()
    keys = {"anthropic": os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"),
            "groq": config.GROQ_API_KEY, "gemini": config.GEMINI_API_KEY}
    if provider == "ollama":
        return _item("Model", _reaches("http://127.0.0.1:11434/api/tags"), "ollama", "Start Ollama.")
    return _item("Model", bool(keys.get(provider)), provider, f"Add the {provider.upper()} API key to .env.")


def checks():
    count = _shop_containers()
    action = (config.ACTION_API_URL or "http://127.0.0.1:8099").rstrip("/") + "/health"
    baselines = config.BASELINE_FILE.exists() and (config.ROOT / "baseline.json").exists()
    email_ready = config.EMAIL_ENABLED and bool(config.EMAIL_TO)
    from oncall.ui import auth
    return [
        _item("Python", sys.version_info >= (3, 11), sys.version.split()[0], "Install Python 3.11 or newer."),
        _item("Packages", all(importlib.util.find_spec(p) for p in ("requests", "anthropic", "openai")), "installed",
              "Run: python scripts/bootstrap.py"),
        _item("Settings file", (config.ROOT / ".env").exists(), ".env", "Run: python scripts/bootstrap.py"),
        _model(),
        _item("Dashboard account", bool(auth.users()), f"{len(auth.users())} account(s)",
              "Run: python -m oncall user add yourname --role admin"),
        _item("Kill switch", not config.KILL_SWITCH_FILE.exists(), "off", "Run: python -m oncall resume"),
        _item("Docker", count is not None, "reachable", "Start Docker Desktop."),
        _item("Shop", (count or 0) >= SHOP_MINIMUM, f"{count} containers running",
              "Start the shop: see Run it for real in the README."),
        _item("Monitoring", _reaches(config.PROMETHEUS_URL + "/-/ready"), "Prometheus reachable",
              "Start the shop with compose.observability.yaml."),
        _item("Healthy baseline", baselines, "recorded",
              "With the shop healthy, run: python -m oncall baseline --window 5m, then python detector.py --calibrate 300"),
        _item("Action API token", bool(config.ACTION_API_TOKEN), "set", "Run: python scripts/bootstrap.py"),
        _item("Action API", _reaches(action), "running", "Run: python -m oncall up", required=False),
        _item("Dashboard approvals", config.UI_APPROVALS, "on", "Add UI_APPROVALS=on to .env.", required=False),
        _item("Email alerts", email_ready, "on", "Add the email settings to .env; see the README.", required=False),
        _item("Phone calls", config.CALLS_ENABLED, "on", "Add CALLS_ENABLED=on and the Twilio settings to .env.", required=False),
    ]


def report():
    """The checks as printable lines, and whether anything required is missing."""
    found = checks()
    lines = [f"[{i['state'].upper():8}] {i['name']}: {i['detail']}" + (f"\n           -> {i['fix']}" if i["fix"] else "") for i in found]
    missing = [i for i in found if i["state"] == FIX]
    lines.append("\nReady." if not missing else f"\n{len(missing)} thing(s) to fix before a live run.")
    return lines, bool(missing)
