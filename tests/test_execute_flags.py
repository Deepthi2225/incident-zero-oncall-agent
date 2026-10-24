"""--execute needs a human, and the approval box rings the bell (based on Aniruddha's aj_ck_3 branch)."""
import subprocess
import sys

import pytest

from oncall import config
from oncall.agent.run import EXECUTE_NEEDS_HUMAN
from oncall.remediate import make_approver
from tests.test_remediate import plan_for

MESSAGE = "--execute requires a human approval; --auto-approve only works with dry runs."


def run_cli(*args):
    return subprocess.run([sys.executable, *args], cwd=config.ROOT, capture_output=True, text=True)


@pytest.mark.parametrize("flag", ["--auto-approve", "--yes", "-y"])
def test_agent_cli_refuses_execute_with_auto_approve(flag):
    r = run_cli("-m", "oncall.agent.run", "alert.json", "--execute", flag)
    assert r.returncode == 2 and MESSAGE in r.stderr and EXECUTE_NEEDS_HUMAN == MESSAGE


@pytest.mark.parametrize("flag", ["--auto-approve", "--yes", "-y"])
def test_demo_refuses_execute_with_auto_approve_before_injecting(flag):
    r = run_cli("scripts/demo.py", "stop-payment", "--execute", flag)
    assert r.returncode == 2 and MESSAGE in r.stderr
    assert "inject" not in r.stdout  # stopped at startup, nothing injected


def test_each_flag_alone_is_still_accepted():
    from argparse import ArgumentParser, Namespace
    from oncall.agent.run import check_execute_flags
    p = ArgumentParser()
    check_execute_flags(p, Namespace(execute=True, auto_approve=False))
    check_execute_flags(p, Namespace(execute=False, auto_approve=True))


def test_bell_rings_just_before_the_approval_box(mem, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    diag = plan_for(("payment", "container_stopped_or_restarting"))
    make_approver()(diag["remediation_plan"]["steps"][0], diag)
    out = capsys.readouterr().out
    assert out.startswith("\a\n" + "=" * 64) and "APPROVAL NEEDED" in out


def test_no_bell_without_a_terminal_or_with_auto_approve(mem, capsys):
    diag = plan_for(("payment", "container_stopped_or_restarting"))
    step = diag["remediation_plan"]["steps"][0]
    make_approver(interactive=False)(step, diag)
    make_approver(auto_approve=True)(step, diag)
    assert "\a" not in capsys.readouterr().out
