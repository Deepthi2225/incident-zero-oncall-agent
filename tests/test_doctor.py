"""First-time setup, the readiness check, and starting everything with one command."""
import importlib.util
import json
from argparse import Namespace
from pathlib import Path

import pytest

from oncall import __main__ as cli
from oncall import config, doctor

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("bootstrap", ROOT / "scripts" / "bootstrap.py")
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


@pytest.mark.parametrize("text,expected", [("A=1\n", "A=1\nX=new\n"), ("X=\n", "X=new\n"), ("# X=old\n", "X=new\n"), ("#X=\nB=2\n", "X=new\nB=2\n")])
def test_bootstrap_fills_in_a_missing_setting(text, expected):
    assert bootstrap.fill_in(text, "X", "new") == expected


def test_bootstrap_never_overwrites_a_setting():
    assert bootstrap.fill_in("X=mine\n", "X", "new") == "X=mine\n"


def test_bootstrap_keeps_an_existing_env(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("ACTION_API_TOKEN=keep\nACTION_API_URL=http://x\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "ROOT", tmp_path)
    assert bootstrap.make_env() is False and "keep" in (tmp_path / ".env").read_text()


@pytest.fixture
def quiet(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "_shop_containers", lambda: None)
    monkeypatch.setattr(doctor, "_reaches", lambda url: False)
    monkeypatch.setattr(config, "UI_USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", tmp_path / "STOP")


def by_name(items):
    return {i["name"]: i for i in items}


def test_doctor_says_what_to_do_about_each_problem(quiet):
    found = by_name(doctor.checks())
    assert found["Docker"]["state"] == doctor.FIX and "Docker Desktop" in found["Docker"]["fix"]
    assert "user add" in found["Dashboard account"]["fix"] and found["Shop"]["state"] == doctor.FIX


def test_optional_things_do_not_block(quiet):
    found = by_name(doctor.checks())
    assert found["Phone calls"]["state"] == doctor.OPTIONAL and found["Action API"]["state"] == doctor.OPTIONAL


def test_doctor_never_shows_a_secret(quiet, monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "gsk_supersecretvalue")
    monkeypatch.setattr(config, "PROVIDER", "groq")
    assert "supersecret" not in json.dumps(doctor.checks()) and by_name(doctor.checks())["Model"]["state"] == doctor.OK


def test_report_counts_what_is_missing(quiet):
    lines, missing = doctor.report()
    assert missing and "to fix before a live run" in lines[-1]


def args(watch=False, execute=False):
    return Namespace(watch=watch, execute=execute)


def test_up_starts_the_api_and_dashboard():
    assert [name for name, _ in cli.up_services(args())] == ["Action API", "dashboard"]


def test_up_runs_the_watcher_dry_unless_asked():
    assert cli.up_services(args(watch=True))[-1][1] == ["oncall", "watch"]
    assert cli.up_services(args(watch=True, execute=True))[-1][1] == ["oncall", "watch", "--execute"]


def test_up_refuses_execute_without_the_watcher(monkeypatch):
    with pytest.raises(SystemExit, match="--watch"):
        cli.cmd_up(args(execute=True))
