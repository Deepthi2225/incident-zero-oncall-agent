"""Offline bundles.

RecordingSource wraps the live source and saves every answer to a bundle directory.
BundleSource replays that directory without the demo running, so the agent and tools can be
developed and re-run on any laptop.

Bundle layout (one JSON file per method, keyed by the call's arguments):
    <dir>/meta.json            {"captured_at": <epoch seconds>, "note": "..."}
    <dir>/<method>.json        {"<args key>": <result>, ...}

All calls take relative windows ("last 300 s"), so keys do not contain timestamps.
Organiser bundles use their own format; convert them into this layout with a small adapter
once the format is published.
"""
import json
import time
from pathlib import Path

METHODS = ["prom_names", "prom_query", "prom_range", "jaeger_services", "jaeger_traces",
           "container_names", "docker_inspect", "docker_stats", "docker_logs"]


def _key(args, kwargs):
    return json.dumps([list(args), kwargs], sort_keys=True, default=str)


class BundleSource:
    name = "bundle"

    def __init__(self, directory):
        self.dir = Path(directory)
        if not self.dir.is_dir():
            raise FileNotFoundError(f"bundle directory not found: {self.dir}")
        meta_file = self.dir / "meta.json"
        self.meta = json.loads(meta_file.read_text()) if meta_file.exists() else {}
        self.detail = f"recorded bundle {self.dir}"
        self._cache = {}

    def now(self):
        return self.meta.get("captured_at", time.time())

    def _lookup(self, method, args, kwargs):
        if method not in self._cache:
            f = self.dir / f"{method}.json"
            self._cache[method] = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        table = self._cache[method]
        key = _key(args, kwargs)
        if key in table:
            return table[key]
        if method == "docker_logs":
            return ""
        return []  # not recorded: behave like "no data"

    def __getattr__(self, method):
        if method not in METHODS:
            raise AttributeError(method)
        return lambda *args, **kwargs: self._lookup(method, args, kwargs)


class RecordingSource:
    """Pass-through to a live source that also writes every answer into a bundle."""

    def __init__(self, inner, directory, note=""):
        self.inner = inner
        self.name = inner.name
        self.detail = f"{inner.detail}; recording to {directory}"
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "meta.json").write_text(json.dumps({"captured_at": time.time(), "note": note}))

    def now(self):
        return self.inner.now()

    def _save(self, method, args, kwargs, result):
        f = self.dir / f"{method}.json"
        table = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        table[_key(args, kwargs)] = result
        f.write_text(json.dumps(table), encoding="utf-8")

    def __getattr__(self, method):
        if method not in METHODS:
            return getattr(self.inner, method)
        fn = getattr(self.inner, method)

        def wrapper(*args, **kwargs):
            result = fn(*args, **kwargs)
            self._save(method, args, kwargs, result)
            return result
        return wrapper
