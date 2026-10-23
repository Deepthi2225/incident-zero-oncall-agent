"""Test source: answers from JSON fixtures shaped exactly like the real API responses.

Only for tests and rehearsals. It is selected only with an explicit --test flag (or
set_source("test", allow_test=True)), and only reads directories under tests/fixtures/.

Fixture directory layout (see tests/fixtures/payment_down):
    meta.json                        {"now": <epoch seconds>, "scenario": "..."}
    baseline.json                    healthy numbers in our baseline format (optional)
    prometheus/label_names.json      GET /api/v1/label/__name__/values response
    prometheus/manifest.json         [{"match": [substr, ...], "file": "x.json", "type": "query"|"range"}]
                                     first entry whose substrings all occur in the PromQL wins
    prometheus/<file>.json           GET /api/v1/query or /query_range response
    jaeger/services.json             GET /api/services response
    jaeger/traces_<service>.json     GET /api/traces?service=<service> response
    docker/inspect.json              `docker inspect <all containers>` output
    docker/stats.jsonl               `docker stats --no-stream --format '{{json .}}'` output
    docker/logs/<container>.log      `docker logs --timestamps` output
"""
import json
from pathlib import Path

from oncall import config
from oncall.sources.guard import allowed, check
from oncall.sources.live import SourceError

FIXTURES_ROOT = config.ROOT / "tests" / "fixtures"


class FixtureSource:
    name = "test"

    def __init__(self, directory):
        self.dir = Path(directory).resolve()
        if not self.dir.is_relative_to(FIXTURES_ROOT.resolve()):
            raise ValueError(f"test fixtures must live under {FIXTURES_ROOT}, not {self.dir}")
        if not self.dir.is_dir():
            raise FileNotFoundError(f"fixture directory not found: {self.dir}")
        self.detail = f"fixtures {self.dir.relative_to(config.ROOT).as_posix()}"
        self.meta = self._json("meta.json", {})

    def _json(self, rel, default=None):
        path = self.dir / rel
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def now(self):
        return self.meta.get("now", 0)

    # --- Prometheus (same unwrapping as the live HTTP responses) ---------------
    def prom_names(self):
        return self._json("prometheus/label_names.json", {"data": []})["data"]

    def _prom(self, query, kind):
        check(query)
        for entry in self._json("prometheus/manifest.json", []):
            if entry.get("type", "query") == kind and all(m in query for m in entry["match"]):
                return self._json(f"prometheus/{entry['file']}")["data"]["result"]
        return []

    def prom_query(self, query):
        return self._prom(query, "query")

    def prom_range(self, query, window_s, step_s):
        return self._prom(query, "range")

    # --- Jaeger ----------------------------------------------------------------
    def jaeger_base(self):
        return f"test:{self.dir}"

    def jaeger_services(self):
        return self._json("jaeger/services.json", {"data": []}).get("data") or []

    def jaeger_traces(self, service, lookback_s, limit, error_only=True):
        traces = self._json(f"jaeger/traces_{service}.json", {"data": []}).get("data") or []
        if error_only:
            traces = [t for t in traces if any(
                tag.get("key") == "error" and tag.get("value") in (True, "true")
                for s in t["spans"] for tag in s.get("tags", []))]
        return traces[:limit]

    # --- Docker ----------------------------------------------------------------
    def container_names(self):
        return [n for n in (i["Name"].lstrip("/") for i in self._json("docker/inspect.json", []))
                if allowed(n)]

    def docker_inspect(self, names=None):
        items = [i for i in self._json("docker/inspect.json", []) if allowed(i["Name"])]
        if names:
            for n in names:
                check(n)
            missing = set(names) - {i["Name"].lstrip("/") for i in items}
            if missing:
                raise SourceError(f"Error: No such object: {sorted(missing)[0]}")
            items = [i for i in items if i["Name"].lstrip("/") in names]
        return items

    def docker_stats(self):
        path = self.dir / "docker" / "stats.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def docker_logs(self, name, since_s, tail=2000):
        check(name)
        path = self.dir / "docker" / "logs" / f"{name}.log"
        if not path.exists() and name not in self.container_names():
            raise SourceError(f"Error response from daemon: No such container: {name}")
        return path.read_text(encoding="utf-8") if path.exists() else ""
