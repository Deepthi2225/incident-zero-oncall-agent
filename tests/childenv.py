"""The environment for child processes that tests start (python -m oncall ..., python -c ...).

A child must see only what the test gives it: never the repo's .env (ONCALL_DOTENV=off), never a
project setting or API key from the developer's shell, so a test can never email, call an LLM or
reach any service because of someone's local configuration.
"""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Every quoted ALL_CAPS name in the modules that read settings counts as a setting.
_SOURCES = ["oncall/config.py", "oncall/record.py", "oncall/notify.py", "actionapi/config.py", "detector.py"]
_NAME = re.compile(r"""["']([A-Z][A-Z0-9_]{2,})["']""")
_PREFIXES = ("ONCALL_", "EMAIL_", "SMTP_", "RESEND_", "NOTIFICATION_", "ACTION_API_", "VERIFY_", "ANTHROPIC_",
             "GEMINI_", "GROQ_", "LOCUST_", "FAULTCTL_")
_KEEP = {"PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR", "HOME",
         "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PYTHONPATH", "PYTHONHOME", "LANG", "LC_ALL"}
SETTINGS = (set().union(*(_NAME.findall((ROOT / p).read_text(encoding="utf-8")) for p in _SOURCES))
            | {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "GEMINI_API_KEY", "RESEND_API_KEY",
               "NOTIFY_ALL_CHANNELS"}) - _KEEP


def isolated_env(**settings):
    """os.environ without any project setting or key, with ONCALL_DOTENV=off, plus settings."""
    env = {k: v for k, v in os.environ.items() if k not in SETTINGS and not k.startswith(_PREFIXES)}
    env.update(ONCALL_DOTENV="off", PYTHONIOENCODING="utf-8")
    env.update({k: str(v) for k, v in settings.items()})
    return env
