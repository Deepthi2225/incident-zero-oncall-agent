"""Live data source: Prometheus, Jaeger and the docker CLI. Read-only."""
import json
import subprocess
import time

import requests

from oncall import config
from oncall.sources.guard import allowed, check
from oncall.sources.jaeger_v3 import has_error, otlp_to_jaeger
from oncall.sources.sanitize import contains_flags, strip_flags


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


def _severity_filter(min_severity):
    """OTel severity >= min. Records without a severity (e.g. Envoy access logs) count as info."""
    if not min_severity:
        return {"match_all": {}}
    should = [{"range": {"severity.number": {"gte": min_severity}}}]
    if min_severity <= 9:
        should.append({"bool": {"must_not": {"exists": {"field": "severity.number"}}}})
    return {"bool": {"should": should, "minimum_should_match": 1}}


class LiveSource:
    name = "live"

    def __init__(self):
        self._jaeger_base = None
        self._opensearch = None
        self.detail = f"prometheus {config.PROMETHEUS_URL}, jaeger, opensearch, docker CLI"

    def now(self):
        return time.time()

    # --- Prometheus ----------------------------------------------------------
    def prom_names(self):
        return _get(f"{config.PROMETHEUS_URL}/api/v1/label/__name__/values")["data"]

    def prom_query(self, query):
        return strip_flags(_get(f"{config.PROMETHEUS_URL}/api/v1/query", {"query": query})["data"]["result"])

    def prom_range(self, query, window_s, step_s):
        end = self.now()
        params = {"query": query, "start": end - window_s, "end": end, "step": step_s}
        return strip_flags(_get(f"{config.PROMETHEUS_URL}/api/v1/query_range", params)["data"]["result"])

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
        return strip_flags(stdout + stderr)

    # --- OpenSearch (logs shipped over OTLP by the collector) -------------------
    def opensearch_base(self):
        """OPENSEARCH_URL, else the host port docker published for the opensearch container.
        Returns None when OpenSearch is not running (search_logs then uses docker logs)."""
        if self._opensearch:
            return self._opensearch
        base = config.OPENSEARCH_URL
        if not base:
            try:
                stdout, _ = _docker("port", "opensearch", "9200", timeout=10)
            except SourceError:
                return None
            port = stdout.strip().splitlines()[0].rsplit(":", 1)[-1] if stdout.strip() else ""
            base = f"http://localhost:{port}" if port else ""
        try:
            if base and requests.get(base, timeout=3).ok:
                self._opensearch = base
        except requests.RequestException:
            pass
        return self._opensearch

    def opensearch_logs(self, service, since_s, min_severity=0, size=500):
        """Recent log records of one service, newest first, or None if OpenSearch is unavailable."""
        check(service)
        base = self.opensearch_base()
        if not base:
            return None
        query = {
            "size": size, "sort": [{"@timestamp": "desc"}],
            "_source": ["@timestamp", "severity", "body", "traceId"],
            "query": {"bool": {"filter": [
                {"term": {"resource.service.name.keyword": service}},
                {"range": {"@timestamp": {"gte": f"now-{int(since_s)}s"}}},
                _severity_filter(min_severity),
            ]}},
        }
        url = f"{base}/{config.OPENSEARCH_INDEX}/_search"
        check(url)
        try:
            resp = requests.post(url, json=query, timeout=config.HTTP_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise SourceError(f"POST {url} failed: {e}") from e
        docs = []
        for hit in resp.json().get("hits", {}).get("hits", []):
            src = hit.get("_source", {})
            sev = src.get("severity") or {}
            doc = {"ts": src.get("@timestamp"), "severity_number": sev.get("number"),
                   "severity_text": sev.get("text"), "body": src.get("body"),
                   "trace_id": src.get("traceId")}
            if not contains_flags(doc):  # HARD RULE 2: never pass flag data on
                docs.append(doc)
        return docs
