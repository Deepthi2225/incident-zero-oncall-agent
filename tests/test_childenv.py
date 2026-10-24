"""Tests never depend on, or act on, the developer's .env: ONCALL_DOTENV=off skips the repo's .env
(oncall and actionapi), and child processes get an environment without local settings or keys."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from actionapi import config as api_config
from oncall import config
from tests.childenv import isolated_env

DOTENV = ("EMAIL_ENABLED=on\nEMAIL_TRANSPORT=resend\nRESEND_API_KEY=re_from_dotenv_000000000000\n"
          "NOTIFICATION_FROM_EMAIL=dotenv@example.test\nONCALL_PRIMARY_EMAIL=dotenv@example.test\n"
          "SMTP_PASS=from-dotenv\nACTION_API_TOKEN=token-from-dotenv\n")
NAMES = ["EMAIL_ENABLED", "EMAIL_TRANSPORT", "RESEND_API_KEY", "NOTIFICATION_FROM_EMAIL", "ONCALL_PRIMARY_EMAIL",
         "SMTP_PASS", "ACTION_API_TOKEN", "ONCALL_DOTENV", "EMAIL_FROM", "EMAIL_TO", "SMTP_PASSWORD"]


def evaluate(module, monkeypatch, dotenv):
    """module's source run with __file__ in a temporary repo whose .env is DOTENV."""
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)
    if dotenv is not None:
        monkeypatch.setenv("ONCALL_DOTENV", dotenv)
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / ".env").write_text(DOTENV, encoding="utf-8")
        fake = Path(tmp, *Path(module.__file__).parts[-2:])
        ns = {"__file__": str(fake), "__name__": f"_isolated_{module.__name__}"}
        exec(compile(Path(module.__file__).read_text(encoding="utf-8"), module.__file__, "exec"), ns)
    for name in NAMES:  # load_dotenv writes into os.environ; leave nothing behind
        monkeypatch.delenv(name, raising=False)
    return ns


@pytest.mark.parametrize("value", ["off", "0", "false", "OFF"])
def test_oncall_dotenv_off_skips_the_repo_env(monkeypatch, value):
    c = evaluate(config, monkeypatch, value)
    assert (c["EMAIL_ENABLED"], c["EMAIL_TRANSPORT"], c["RESEND_API_KEY"], c["EMAIL_FROM"], c["EMAIL_TO"],
            c["SMTP_PASSWORD"]) == (False, "smtp", "", "", [], "")


@pytest.mark.parametrize("value", [None, "on"])
def test_the_repo_env_is_read_by_default(monkeypatch, value):
    c = evaluate(config, monkeypatch, value)
    assert (c["EMAIL_TRANSPORT"], c["EMAIL_FROM"], c["SMTP_PASSWORD"]) == ("resend", "dotenv@example.test", "from-dotenv")


def test_action_api_honours_it_too(monkeypatch):
    assert evaluate(api_config, monkeypatch, "off")["TOKEN"] == ""
    assert evaluate(api_config, monkeypatch, None)["TOKEN"] == "token-from-dotenv"


def test_isolated_env_drops_settings_and_keys(monkeypatch):
    for name, value in [("RESEND_API_KEY", "re_shell_key_0000000000"), ("EMAIL_TRANSPORT", "resend"),
                        ("NOTIFICATION_FROM_EMAIL", "a@example.test"), ("ONCALL_PRIMARY_EMAIL", "b@example.test"),
                        ("SMTP_PASS", "pw"), ("ANTHROPIC_API_KEY", "sk-ant-shell"), ("NOTIFY_ALL_CHANNELS", "true"),
                        ("ONCALL_REVIEWER", "on"), ("ACTION_API_URL", "http://127.0.0.1:8099")]:
        monkeypatch.setenv(name, value)
    env = isolated_env(EMAIL_TO="x@example.test")
    assert env["ONCALL_DOTENV"] == "off" and env["EMAIL_TO"] == "x@example.test" and "PATH" in env
    for name in ("RESEND_API_KEY", "EMAIL_TRANSPORT", "NOTIFICATION_FROM_EMAIL", "ONCALL_PRIMARY_EMAIL", "SMTP_PASS",
                 "ANTHROPIC_API_KEY", "NOTIFY_ALL_CHANNELS", "ONCALL_REVIEWER", "ACTION_API_URL"):
        assert name not in env, name


def test_a_child_process_sees_none_of_the_local_configuration(monkeypatch):
    """End to end: whatever the repo .env and the shell say, the child loads only defaults."""
    monkeypatch.setenv("EMAIL_TRANSPORT", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "re_shell_key_0000000000")
    script = ("import json\nfrom oncall import config\n"
              "print(json.dumps({'enabled': config.EMAIL_ENABLED, 'transport': config.EMAIL_TRANSPORT,"
              " 'resend': bool(config.RESEND_API_KEY), 'from': config.EMAIL_FROM, 'to': config.EMAIL_TO,"
              " 'smtp_pw': bool(config.SMTP_PASSWORD), 'reviewer': config.REVIEWER,"
              " 'action_api': config.ACTION_API_URL}))")
    out = subprocess.run([sys.executable, "-c", script], cwd=config.ROOT, env=isolated_env(),
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {"enabled": False, "transport": "smtp", "resend": False, "from": "", "to": [],
                                      "smtp_pw": False, "reviewer": False, "action_api": ""}


def test_this_test_process_did_not_read_the_repo_env():
    import os
    assert os.environ.get("ONCALL_DOTENV") == "off"
