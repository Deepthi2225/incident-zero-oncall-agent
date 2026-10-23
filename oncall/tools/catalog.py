"""Tool catalogue for the agent: Anthropic tool definitions + a dispatcher.

All tools are read-only. Each returns a small dict and registers its findings in the evidence
registry; the IDs it returns ("evidence": "E7") are what the diagnosis must cite.
"""
from oncall.tools.containers import get_container_health
from oncall.tools.deploys import get_deploy_history
from oncall.tools.logs import search_logs
from oncall.tools.metrics import get_metric, list_metrics
from oncall.tools.service_health import get_service_health
from oncall.tools.traces import find_error_traces, list_trace_services

FUNCTIONS = {
    "get_service_health": get_service_health,
    "get_metric": get_metric,
    "list_metrics": list_metrics,
    "find_error_traces": find_error_traces,
    "list_trace_services": list_trace_services,
    "search_logs": search_logs,
    "get_container_health": get_container_health,
    "get_deploy_history": get_deploy_history,
}

_WINDOW = {"type": "string", "description": "Relative window such as 90s, 5m, 1h."}


def _schema(props, required=()):
    return {"type": "object", "properties": props, "required": list(required),
            "additionalProperties": False}


TOOL_SPECS = [
    {
        "name": "get_service_health",
        "description": "Per-service request rate, server error rate, client-call error rate and p95 "
                       "latency from span metrics, compared with the healthy baseline. Returns "
                       "flagged services worst first (flags: errors, client_call_errors, slow, "
                       "traffic_spike, traffic_drop, silent = served traffic in baseline but no "
                       "spans now). Usually the first call.",
        "input_schema": _schema({"window": _WINDOW}),
    },
    {
        "name": "find_error_traces",
        "description": "Analyse recent Jaeger traces for one service. Returns where failures "
                       "ORIGINATE (deepest failing span, its message, call chain), failed client "
                       "calls that got no server span (callee unreachable), span attribute values "
                       "that failing spans share, and client-vs-server duration per edge (network "
                       "delay). Set errors_only=false with min_duration_ms to study slow requests.",
        "input_schema": _schema({
            "service": {"type": "string", "description": "Service name as in Jaeger, e.g. frontend."},
            "lookback": _WINDOW,
            "limit": {"type": "integer", "description": "Traces to fetch, max 50 (default 20)."},
            "errors_only": {"type": "boolean"},
            "min_duration_ms": {"type": "number"},
        }, ["service"]),
    },
    {
        "name": "list_trace_services",
        "description": "Services that currently report traces to Jaeger.",
        "input_schema": _schema({}),
    },
    {
        "name": "search_logs",
        "description": "Grouped log patterns (count, first/last seen, example line) from docker "
                       "logs for up to 5 services. Container name equals service name.",
        "input_schema": _schema({
            "services": {"type": "array", "items": {"type": "string"}},
            "since": _WINDOW,
            "level": {"type": "string", "enum": ["debug", "info", "warn", "error"],
                      "description": "Minimum level (default warn)."},
            "contains": {"type": "string", "description": "Case-insensitive substring filter."},
        }, ["services"]),
    },
    {
        "name": "get_container_health",
        "description": "Docker state for every container (or the named ones): running or not, "
                       "OOMKilled, restart count, health check, uptime, CPU and memory vs limit.",
        "input_schema": _schema({"names": {"type": "array", "items": {"type": "string"}}}),
    },
    {
        "name": "get_deploy_history",
        "description": "Recent changes: containers created or (re)started in the lookback, "
                       "organiser deploy history if configured, and our own past actions.",
        "input_schema": _schema({"lookback": _WINDOW}),
    },
    {
        "name": "list_metrics",
        "description": "Find Prometheus metric names containing all given words, e.g. "
                       "'memory', 'gc', 'cpu', 'jvm'. Use before get_metric for runtime metrics.",
        "input_schema": _schema({"contains": {"type": "string"}}),
    },
    {
        "name": "get_metric",
        "description": "Run a PromQL range query and get min/max/first/latest/trend per series "
                       "(max 8 series). Use for memory growth, CPU, GC, request rate over time.",
        "input_schema": _schema({"promql": {"type": "string"}, "window": _WINDOW}, ["promql"]),
    },
]


def dispatch(name, args):
    """Run a tool. Returns (result_dict, is_error)."""
    fn = FUNCTIONS.get(name)
    if fn is None:
        return {"error": f"unknown tool {name}"}, True
    try:
        return fn(**(args or {})), False
    except Exception as e:  # tools must never crash the agent loop
        return {"error": f"{type(e).__name__}: {str(e)[:300]}"}, True
