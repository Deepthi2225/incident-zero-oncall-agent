"""Jaeger 2.x API v3 (OTLP JSON) -> the Jaeger v1 trace shape our trace tool analyses.

Jaeger 2.21 in the demo serves only /api/v3 (the legacy /api/services and /api/traces return 404).
v3 groups spans by resource (service) and uses OTLP enums, so this module regroups spans by
trace and maps kinds, status and events to v1-style tags and logs.

HARD RULE 2: spans from flag services (flagd, flagd-ui) are dropped, and so are feature_flag.*
attributes and feature_flag events on every other span, so flag state never reaches the agent.
"""
from oncall.sources.guard import allowed

KINDS = {1: "internal", 2: "server", 3: "client", 4: "producer", 5: "consumer",
         "SPAN_KIND_INTERNAL": "internal", "SPAN_KIND_SERVER": "server",
         "SPAN_KIND_CLIENT": "client", "SPAN_KIND_PRODUCER": "producer",
         "SPAN_KIND_CONSUMER": "consumer"}
ERROR_CODES = (2, "STATUS_CODE_ERROR")


def _value(v):
    """OTLP AnyValue -> plain Python value."""
    if not isinstance(v, dict):
        return v
    for key in ("stringValue", "boolValue", "doubleValue"):
        if key in v:
            return v[key]
    if "intValue" in v:
        return int(v["intValue"])
    if "arrayValue" in v:
        return [_value(x) for x in v["arrayValue"].get("values", [])]
    return None


def _is_flag(key):
    return str(key).startswith("feature_flag")


def _tags(attributes):
    return [{"key": a["key"], "value": _value(a.get("value"))}
            for a in attributes or [] if not _is_flag(a["key"])]


def _service(resource):
    for a in resource.get("attributes", []):
        if a["key"] == "service.name":
            return _value(a["value"])
    return "unknown"


def otlp_to_jaeger(result):
    """v3 response body['result'] -> list of v1-style traces ({traceID, spans, processes})."""
    traces = {}
    for rs in (result or {}).get("resourceSpans", []):
        service = _service(rs.get("resource", {}))
        if not allowed(service):
            continue
        for ss in rs.get("scopeSpans", []):
            for s in ss.get("spans", []):
                trace = traces.setdefault(s["traceId"], {"traceID": s["traceId"], "spans": [],
                                                          "processes": {}, "warnings": None})
                pid = next((p for p, info in trace["processes"].items()
                            if info["serviceName"] == service), None)
                if pid is None:
                    pid = f"p{len(trace['processes']) + 1}"
                    trace["processes"][pid] = {"serviceName": service, "tags": []}
                tags = _tags(s.get("attributes"))
                tags.append({"key": "span.kind", "value": KINDS.get(s.get("kind"), "unspecified")})
                status = s.get("status") or {}
                if status.get("code") in ERROR_CODES:
                    tags += [{"key": "error", "value": True},
                             {"key": "otel.status_code", "value": "ERROR"}]
                    if status.get("message"):
                        tags.append({"key": "otel.status_description", "value": status["message"]})
                logs = [{"timestamp": int(e.get("timeUnixNano", 0)) // 1000,
                         "fields": [{"key": "event", "value": e.get("name")}] + _tags(e.get("attributes"))}
                        for e in s.get("events") or [] if not _is_flag(e.get("name", ""))]
                start = int(s.get("startTimeUnixNano", 0))
                end = int(s.get("endTimeUnixNano", start))
                parent = s.get("parentSpanId")
                trace["spans"].append({
                    "traceID": s["traceId"], "spanID": s["spanId"], "operationName": s.get("name"),
                    "references": [{"refType": "CHILD_OF", "traceID": s["traceId"], "spanID": parent}]
                    if parent else [],
                    "startTime": start // 1000, "duration": max(end - start, 0) // 1000,
                    "tags": tags, "logs": logs, "processID": pid,
                })
    # Newest first, like the v1 search API.
    return sorted(traces.values(), key=lambda t: -max((sp["startTime"] for sp in t["spans"]), default=0))


def has_error(trace):
    return any(t["key"] == "error" and t["value"] in (True, "true")
               for s in trace["spans"] for t in s["tags"])
