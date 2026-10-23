"""find_error_traces: where do failures (or slowness) ORIGINATE?

For each Jaeger trace it finds the deepest failing span (the origin; spans above it are victims
that propagated the error), the call chain to it, and client-vs-server duration per hop
(a big gap between A's client span and B's server span points at the network between them).

CLI: python -m oncall.tools.traces frontend --lookback 15m
     python -m oncall.tools.traces checkout --all --min-duration-ms 500
"""
import argparse
from collections import Counter, defaultdict

from oncall.evidence import registry
from oncall.sources import get_source
from oncall.tools._common import cap, normalise, parse_window, run_cli

TOOL = "find_error_traces"
PEER_KEYS = ["peer.service", "rpc.service", "server.address", "net.peer.name", "db.system",
             "http.host", "url.full", "http.url"]
GAP_MIN_MS = 50.0


def _tags(items):
    return {t["key"]: t.get("value") for t in items or []}


def parse_trace(trace):
    processes = trace.get("processes", {})
    spans = {}
    for s in trace.get("spans", []):
        tags = _tags(s.get("tags"))
        message = tags.get("otel.status_description") or tags.get("error.message")
        for log in s.get("logs") or []:
            fields = _tags(log.get("fields"))
            message = message or fields.get("exception.message") or fields.get("message")
        parent = next((r["spanID"] for r in s.get("references") or []
                       if r.get("refType") == "CHILD_OF"), None)
        spans[s["spanID"]] = {
            "id": s["spanID"], "parent": parent,
            "service": processes.get(s.get("processID"), {}).get("serviceName", "?"),
            "op": s.get("operationName"),
            "kind": tags.get("span.kind", ""),
            "start": s.get("startTime", 0),
            "duration_ms": s.get("duration", 0) / 1000.0,
            "error": tags.get("error") in (True, "true") or tags.get("otel.status_code") == "ERROR",
            "message": message,
            "peer": next((str(tags[k]) for k in PEER_KEYS if tags.get(k)), None),
            "attrs": {k: v for k, v in tags.items() if k.startswith("app.")},
        }
    for sp in spans.values():  # parents outside this trace become roots
        if sp["parent"] not in spans:
            sp["parent"] = None
    return spans


def analyze_trace(trace):
    spans = parse_trace(trace)
    children = defaultdict(list)
    for sp in spans.values():
        if sp["parent"]:
            children[sp["parent"]].append(sp["id"])

    def depth(sid):
        d = 0
        while spans[sid]["parent"]:
            sid, d = spans[sid]["parent"], d + 1
        return d

    def has_error_below(sid):
        stack = list(children[sid])
        while stack:
            c = stack.pop()
            if spans[c]["error"]:
                return True
            stack.extend(children[c])
        return False

    origins = [sp for sp in spans.values() if sp["error"] and not has_error_below(sp["id"])]
    origins.sort(key=lambda sp: depth(sp["id"]), reverse=True)
    for o in origins:
        # A failed client call with no server span below it: the callee never answered.
        o["no_server_span"] = o["kind"] == "client" and not children[o["id"]]

    def chain(sid):
        path = []
        while sid:
            svc = spans[sid]["service"]
            if not path or path[-1] != svc:
                path.append(svc)
            sid = spans[sid]["parent"]
        return list(reversed(path))

    hops = []
    for sp in spans.values():
        if sp["kind"] != "client":
            continue
        for cid in children[sp["id"]]:
            c = spans[cid]
            if c["kind"] in ("server", "consumer") and c["service"] != sp["service"]:
                hops.append({"from": sp["service"], "to": c["service"],
                             "client_ms": sp["duration_ms"], "server_ms": c["duration_ms"]})
    roots = [sp for sp in spans.values() if not sp["parent"]]
    root = max(roots, key=lambda sp: sp["duration_ms"]) if roots else None
    return {
        "trace_id": trace.get("traceID"),
        "root": f"{root['service']} {root['op']}" if root else None,
        "duration_ms": round(root["duration_ms"], 1) if root else None,
        "origins": origins,
        "chain": chain(origins[0]["id"]) if origins else [],
        "hops": hops,
    }


def find_error_traces(service, lookback="15m", limit=20, errors_only=True, min_duration_ms=None):
    lookback_s = parse_window(lookback, default=900)
    limit = max(1, min(int(limit), 50))
    raw = get_source().jaeger_traces(service, lookback_s, limit, error_only=errors_only)
    analysed = [analyze_trace(t) for t in raw]
    if min_duration_ms:
        analysed = [a for a in analysed if (a["duration_ms"] or 0) >= float(min_duration_ms)]

    # Group failures by where they originate.
    groups = {}
    attr_values = defaultdict(Counter)
    for a in analysed:
        seen = set()
        for o in a["origins"]:
            key = (o["service"], o["op"], normalise(o["message"] or "", 120),
                   o["peer"] if o["no_server_span"] else None)
            if key in seen:
                continue
            seen.add(key)
            g = groups.setdefault(key, {"count": 0, "example_trace": a["trace_id"],
                                        "example_message": (o["message"] or "")[:200],
                                        "chain": a["chain"]})
            g["count"] += 1
            for k, v in o["attrs"].items():
                attr_values[(o["service"], k)][str(v)] += 1

    origin_rows = []
    for (svc, op, msg, peer), g in sorted(groups.items(), key=lambda kv: -kv[1]["count"])[:6]:
        row = {"origin_service": svc, "operation": op, "count": g["count"],
               "message": g["example_message"], "chain": g["chain"],
               "example_trace": g["example_trace"]}
        if peer:
            row["unanswered_call_to"] = peer
        text = (f"{g['count']}/{len(analysed)} traces: deepest failing span is {svc} '{op}'"
                f"{' (client call to ' + peer + ' got no server span)' if peer else ''}"
                f" msg='{g['example_message'][:120]}' chain={'>'.join(g['chain'])}"
                f" e.g. trace {g['example_trace']}")
        row["evidence"] = registry.add(TOOL, "trace", text, {**row, "lookback_s": lookback_s})
        origin_rows.append(row)

    # Errors that cluster on one attribute value (e.g. one product id).
    clusters = []
    for (svc, key), counter in attr_values.items():
        value, n = counter.most_common(1)[0]
        total = sum(counter.values())
        if n >= 3 and n / total >= 0.8:
            c = {"service": svc, "attribute": key, "value": value, "share": round(n / total, 2),
                 "count": n}
            c["evidence"] = registry.add(TOOL, "trace",
                                         f"{n}/{total} failing {svc} spans have {key}={value}", c)
            clusters.append(c)

    # Per-edge client vs server time.
    edges = defaultdict(list)
    for a in analysed:
        for h in a["hops"]:
            edges[(h["from"], h["to"])].append(h)
    edge_rows = []
    for (src, dst), hs in edges.items():
        client = sum(h["client_ms"] for h in hs) / len(hs)
        server = sum(h["server_ms"] for h in hs) / len(hs)
        edge_rows.append({"edge": f"{src}->{dst}", "calls": len(hs), "avg_client_ms": round(client, 1),
                          "avg_server_ms": round(server, 1), "avg_gap_ms": round(client - server, 1)})
    edge_rows.sort(key=lambda e: e["avg_gap_ms"], reverse=True)
    for e in edge_rows[:3]:
        if e["avg_gap_ms"] >= GAP_MIN_MS and e["avg_gap_ms"] >= 0.5 * e["avg_client_ms"]:
            e["evidence"] = registry.add(
                TOOL, "trace",
                f"edge {e['edge']}: client span avg {e['avg_client_ms']}ms vs server span avg "
                f"{e['avg_server_ms']}ms (gap {e['avg_gap_ms']}ms over {e['calls']} calls)", e)

    result = {"service": service, "lookback_s": lookback_s, "errors_only": errors_only,
              "traces_examined": len(analysed), "origins": origin_rows,
              "attribute_clusters": clusters, "edges": edge_rows[:5],
              "slowest": sorted(({"trace_id": a["trace_id"], "root": a["root"],
                                  "duration_ms": a["duration_ms"]} for a in analysed),
                                key=lambda t: -(t["duration_ms"] or 0))[:3]}
    if not analysed:
        what = "error traces" if errors_only else "traces"
        result["evidence"] = registry.add(TOOL, "trace",
                                          f"no {what} for service {service} in last {lookback_s}s",
                                          {"service": service, "lookback_s": lookback_s})
    return cap(result)


def list_trace_services():
    return {"services": sorted(get_source().jaeger_services())}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("service", nargs="?", help="omit to list services")
    p.add_argument("--lookback", default="15m")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--all", action="store_true", help="include non-error traces")
    p.add_argument("--min-duration-ms", type=float)

    def call(**kw):
        if not kw["service"]:
            return list_trace_services()
        return find_error_traces(**kw)

    run_cli(call, p, lambda a: {"service": a.service, "lookback": a.lookback, "limit": a.limit,
                                "errors_only": not a.all, "min_duration_ms": a.min_duration_ms})


if __name__ == "__main__":
    main()
