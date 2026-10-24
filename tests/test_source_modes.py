"""Source selection: live by default, fixtures only with an explicit --test, source on every report."""
import json
import subprocess
import sys

import pytest

from oncall import config, sources
from oncall.sources import TestModeRefused, set_source, source_info
from oncall.sources.live import LiveSource
from oncall.tools import metrics
from tests.childenv import isolated_env
from tests.conftest import FIXTURES

ROOT = config.ROOT


@pytest.fixture(autouse=True)
def reset_source(monkeypatch):
    monkeypatch.setattr(sources, "_source", None)
    yield
    sources._source = None


def test_default_source_is_live(monkeypatch):
    monkeypatch.setattr(config, "SOURCE", "live")
    assert isinstance(sources.get_source(), LiveSource)
    assert source_info()["source"] == "live"


def test_test_mode_needs_explicit_opt_in(monkeypatch):
    with pytest.raises(TestModeRefused):
        set_source("test")
    monkeypatch.setattr(config, "SOURCE", "test")  # e.g. ONCALL_SOURCE=test in the environment
    with pytest.raises(TestModeRefused):
        sources.get_source()
    with pytest.raises(ValueError):
        set_source("fake")  # old name is gone


def test_fixtures_must_live_under_tests_fixtures(tmp_path):
    (tmp_path / "meta.json").write_text("{}")
    with pytest.raises(ValueError, match="tests"):
        set_source("test", fixture_dir=tmp_path, allow_test=True)
    src = set_source("test", fixture_dir=FIXTURES / "payment_down", allow_test=True)
    assert source_info() == {"source": "test", "source_detail": "fixtures tests/fixtures/payment_down"}
    assert src.name == "test"


def test_baseline_is_live_only_and_never_leaks(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "BASELINE_FILE", tmp_path / "baseline.json")
    set_source("test", fixture_dir=FIXTURES / "payment_down", allow_test=True)
    with pytest.raises(RuntimeError, match="live only"):
        metrics.save_baseline()
    assert metrics.load_baseline()["window_s"] == 300  # the fixture's own baseline

    # A baseline file not captured live is ignored by a live run.
    config.BASELINE_FILE.write_text(json.dumps({"source": "test", "services": {}}))
    set_source("live")
    assert metrics.load_baseline() is None
    config.BASELINE_FILE.write_text(json.dumps({"source": "live", "services": {}}))
    assert metrics.load_baseline() == {"source": "live", "services": {}}


def run_cli(*args):
    return subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True,
                          env=isolated_env())  # no .env, no local settings or keys


def test_cli_flags():
    r = run_cli("-m", "oncall.agent.run", "x.json", "--fixtures", "tests/fixtures/payment_down")
    assert r.returncode == 2 and "--fixtures needs --test" in r.stderr
    r = run_cli("-m", "oncall.agent.run", "x.json", "--source", "fake")
    assert r.returncode == 2 and "invalid choice" in r.stderr
    r = run_cli("-m", "oncall.agent.run", "x.json", "--test", "--record", "b")
    assert r.returncode == 2 and "cannot be combined" in r.stderr
    r = run_cli("scripts/demo.py", "stop-payment", "--fixtures", "tests/fixtures/payment_down")
    assert r.returncode == 2 and "--fixtures needs --test" in r.stderr


def test_tool_cli_output_records_source():
    r = run_cli("-m", "oncall.tools.deploys", "--test")
    out = json.loads(r.stdout)
    assert out["source"] == "test" and out["source_detail"].startswith("fixtures tests/fixtures/")
