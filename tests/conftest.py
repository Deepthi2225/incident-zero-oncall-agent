import copy
import os
import re
import tempfile
from pathlib import Path

import pytest

import detector
from actionapi import config as api_config
from oncall import config, sources
from oncall.evidence import registry
from oncall.tools import _common

# --- tests never depend on the local .env or the shell environment ----------------------------------
# A teammate's .env (or shell) may turn on the reviewer, email, Gemini, flag toggling, other
# thresholds... Every setting these modules read from the environment is reset to its built-in
# default before each test; tests that need a feature switch it on explicitly with monkeypatch.
ENV_MODULES = [config, api_config, detector]
_ENV_NAME = re.compile(r"""(?:os\.getenv|os\.environ\.get|env_flag)\(\s*["']([A-Z0-9_]+)["']""")
_SECRET_NAME = re.compile(r"(KEY|TOKEN|PASSWORD)$")


def _env_names(module):
    return set(_ENV_NAME.findall(Path(module.__file__).read_text(encoding="utf-8")))


def _builtin_defaults(module):
    """The module's settings with none of its environment variables set and no .env file: its
    source is run once more with __file__ in an empty temporary folder (so ROOT/.env is absent)."""
    src = Path(module.__file__).read_text(encoding="utf-8")
    names = _env_names(module)
    saved = {k: os.environ.pop(k) for k in list(os.environ) if k in names}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp, *Path(module.__file__).parts[-2:])
            ns = {"__file__": str(fake), "__name__": f"_defaults_{module.__name__}"}
            exec(compile(src, module.__file__, "exec"), ns)
    finally:
        os.environ.update(saved)
    # paths (runs/, STOP, fixtures...) are redirected by the fixtures below, not reset here
    return {k: v for k, v in ns.items() if k.isupper() and k != "ROOT" and not isinstance(v, Path)
            and not callable(v)}


DEFAULTS = {m: _builtin_defaults(m) for m in ENV_MODULES}
ENV_NAMES = sorted(set().union(*(_env_names(m) for m in ENV_MODULES)) | {"ONCALL_NOTIFY_LOG", "ACTION_API_LOG"})


@pytest.fixture(autouse=True)
def builtin_settings(monkeypatch):
    """Reviewer off, email off, provider anthropic, flag toggling off, default thresholds..."""
    for module, values in DEFAULTS.items():
        for name, value in values.items():
            monkeypatch.setattr(module, name, copy.deepcopy(value))
    for name in ENV_NAMES:  # read again at run time by some code paths
        if not _SECRET_NAME.search(name):
            monkeypatch.delenv(name, raising=False)
    # never send real email; email tests turn it on against a local fake SMTP server
    monkeypatch.setattr(config, "SMTP_PASSWORD", "")
    assert (config.EMAIL_ENABLED, config.SMTP_HOST, config.EMAIL_ON_RECOVERED) == (False, "", False)
    assert (config.REVIEWER, config.PROVIDER, config.ALLOW_FLAG_TOGGLE) == (False, "anthropic", False)


class MemorySource:
    """In-memory source. Tests fill the attributes they need."""
    name = "mem"
    detail = "in-memory unit-test double"

    def __init__(self):
        self.names = []
        self.instant = {}      # substring of query -> list of series
        self.range = []
        self.traces = []
        self.logs = {}
        self.inspect = []
        self.stats = []
        self.t = 1_800_000_000.0

    def now(self):
        return self.t

    def prom_names(self):
        return self.names

    def prom_query(self, query):
        # Longest matching key wins so "err" filters beat plain selectors.
        for key in sorted(self.instant, key=len, reverse=True):
            if key in query:
                return self.instant[key]
        return []

    def prom_range(self, query, window_s, step_s):
        return self.range

    def jaeger_services(self):
        return ["frontend", "checkout"]

    def jaeger_traces(self, service, lookback_s, limit, error_only=True):
        return self.traces

    def container_names(self):
        return [i["Name"].lstrip("/") for i in self.inspect]

    def docker_inspect(self, names=None):
        return self.inspect

    def docker_stats(self):
        return self.stats

    def docker_logs(self, name, since_s, tail=2000):
        return self.logs.get(name, "")


@pytest.fixture
def mem(monkeypatch, tmp_path):
    src = MemorySource()
    monkeypatch.setattr(sources, "_source", src)
    monkeypatch.setattr(config, "BASELINE_FILE", tmp_path / "baseline.json")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "ACTION_LOG", tmp_path / "actions.jsonl")
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", tmp_path / "STOP")
    _common._metric_names.clear()
    registry.reset()
    yield src
    _common._metric_names.clear()


FIXTURES = config.ROOT / "tests" / "fixtures"


@pytest.fixture
def payment_down(monkeypatch, tmp_path):
    """Test source over the synthetic payment_down fixtures (payment container stopped)."""
    monkeypatch.setattr(config, "BASELINE_FILE", tmp_path / "baseline.json")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "ACTION_LOG", tmp_path / "actions.jsonl")
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", tmp_path / "STOP")
    _common._metric_names.clear()
    registry.reset()
    src = sources.set_source("test", fixture_dir=FIXTURES / "payment_down", allow_test=True)
    yield src
    sources._source = None
    _common._metric_names.clear()
