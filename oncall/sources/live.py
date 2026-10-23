"""Live data source: Prometheus, Jaeger and the docker CLI. Read-only."""
import json
import subprocess
import time

import requests

from oncall import config
from oncall.sources.guard import allowed, check
from oncall.sources.jaeger_v3 import has_error, otlp_to_jaeger


class SourceError(RuntimeError):
    pass


def _get(url, params=None, empty_on_404=None):
    """GET JSON. empty_on_404: value to return when the API answers 404 for "nothing found"."""
    check(url)
    try:
        resp = requests.get(url, params=params, timeout=config.HTTP_TIMEOUT)
        if resp.status_code == 404 and empty_on_404 is not None:
            return empty_on_404
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        raise SourceError(f"GET {url} failed: {e}") from e


def _docker(*args, timeout=30):
    cmd = ["docker", *args]
    check(" ".join(cmd))
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                             encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SourceError(f"{' '.join(cmd)} failed: {e}") from e
    if out.returncode != 0:
        raise SourceError(f"{' '.join(cmd)} failed: {out.stderr.strip()[:300]}")
    return out.stdout, out.stderr


class LiveSource:
    name = "live"

    def __init__(self):
        self._jaeger_base = None
        self.detail = f"prometheus {config.PROMETHEUS_URL}, jaeger, docker CLI"

    def now(self):
        return time.time()

    # --- Prometheus ----------------------------------------------------------
    def prom_names(self):
        return _get(f"{config.PROMETHEUS_URL}/api/v1/label/__name__/values")["data"]

    def prom_query(self, query):
        return _get(f"{config.PROMETHEUS_URL}/api/v1/query", {"query": query})["data"]["result"]

    def prom_range(self, query, window_s, step_s):
        end = self.now()
        params = {"query": query, "start": end - window_s, "end": end, "step": step_s}
        return _get(f"{config.PROMETHEUS_URL}/api/v1/query_range", params)["data"]["result"]

    # --- Jaeger --------------------------------------------------------------
    def jaeger_base(self):
        if self._jaeger_base:
            return self._jaeger_base
        errors = []
        for base in config.JAEGER_CANDIDATES:
            try:
                _get(f"{base}/services")
                self._jaeger_base = base
                return base
            except SourceError as e:
                errors.append(str(e))
        raise SourceError("no Jaeger API reachable; set JAEGER_URL. " + " | ".join(errors))

    def _v3(self):
        return self.jaeger_base().rstrip("/").endswith("/v3")

    def jaeger_services(self):
        body = _get(f"{self.jaeger_base()}/services")
        services = body.get("services" if self._v3() else "data") or []
        return [s for s in services if allowed(s)]

    def jaeger_traces(self, service, lookback_s, limit, error_only=True):
        check(service)
        if not self._v3():
            end_us = int(self.now() * 1e6)
            params = {"service": service, "limit": limit,
                      "start": end_us - int(lookback_s * 1e6), "end": end_us}
            if error_only:
                params["tags"] = json.dumps({"error": "true"})
            return _get(f"{self.jaeger_base()}/traces", params).get("data") or []

        # v3: attribute filters are unreliable in Jaeger 2.21's memory store, so fetch a deeper
        # batch of recent traces and keep the failing ones ourselves.
        end = self.now()
        fmt = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))  # noqa: E731
        params = {"query.service_name": service,
                  "query.start_time_min": fmt(end - lookback_s), "query.start_time_max": fmt(end + 1),
                  "query.search_depth": min(max(limit * 5, 50), 200) if error_only else limit}
        body = _get(f"{self.jaeger_base()}/traces", params, empty_on_404={"result": {}})
        traces = otlp_to_jaeger(body.get("result"))
        if error_only:
            traces = [t for t in traces if has_error(t)]
        return traces[:limit]

    # --- Docker --------------------------------------------------------------
    def container_names(self):
        args = ["ps", "-a", "--format", "{{.Names}}"]
        if config.COMPOSE_PROJECT:
            args[2:2] = ["--filter", f"label=com.docker.compose.project={config.COMPOSE_PROJECT}"]
        stdout, _ = _docker(*args)
        # Flag containers (flagd, flagd-ui) are never inspected: HARD RULE 2.
        return [n for n in stdout.split() if n and allowed(n)]

    def docker_inspect(self, names=None):
        names = names or self.container_names()
        if not names:
            return []
        stdout, _ = _docker("inspect", *names)
        return json.loads(stdout)

    def docker_stats(self):
        stdout, _ = _docker("stats", "--no-stream", "--format", "{{json .}}", timeout=60)
        rows = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        return [r for r in rows if allowed(r.get("Name", ""))]

    def docker_logs(self, name, since_s, tail=2000):
        # Services write to stdout and stderr; both matter.
        stdout, stderr = _docker("logs", "--timestamps", "--since", f"{int(since_s)}s",
                                 "--tail", str(tail), name)
        return stdout + stderr
