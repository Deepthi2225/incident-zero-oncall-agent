import pytest

from oncall import config, sources
from oncall.evidence import registry
from oncall.tools import _common


class FakeSource:
    """In-memory source. Tests fill the attributes they need."""
    name = "fake"

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
def fake(monkeypatch, tmp_path):
    src = FakeSource()
    monkeypatch.setattr(sources, "_source", src)
    monkeypatch.setattr(config, "BASELINE_FILE", tmp_path / "baseline.json")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "ACTION_LOG", tmp_path / "actions.jsonl")
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", tmp_path / "STOP")
    _common._metric_names.clear()
    registry.reset()
    yield src
    _common._metric_names.clear()
