"""search_logs: grouped log patterns per service.

Reads OpenSearch (logs the collector ships over OTLP) when it is available and has records for
the service, otherwise `docker logs` (some services only log to stdout). Lines are normalised
(ids, numbers, addresses collapsed) and grouped, so the agent sees "connection refused to <ip>
x 412, first/last seen, one example" instead of 412 lines. Each service reports its log_source.

CLI: python -m oncall.tools.logs cart checkout --since 10m --level warn
"""
import argparse
import json
import re
from collections import OrderedDict

from oncall.evidence import registry
from oncall.sources import SourceError, get_source
from oncall.tools._common import cap, normalise, parse_window, run_cli

TOOL = "search_logs"
LEVELS = {"debug": 0, "info": 1, "warn": 2, "error": 3}
OTEL_MIN_SEVERITY = {"debug": 0, "info": 9, "warn": 13, "error": 17}  # OTel severity numbers
TOP_PATTERNS = 15
MAX_SERVICES = 5

_TS = re.compile(r"^(\d{4}-\d\d-\d\dT[\d:.]+Z)\s?(.*)$")
_LEVEL_WORD = re.compile(r"\b(fatal|critical|crit|error|err|severe|warn|warning|info|debug|trace)\b", re.I)
_ERROR_HINT = re.compile(r"(exception|traceback|panic:|unhandled|refused|timed? ?out|unavailable|oom|killed)", re.I)
_WORD_TO_LEVEL = {"fatal": "error", "critical": "error", "crit": "error", "error": "error",
                  "err": "error", "severe": "error", "warn": "warn", "warning": "warn",
                  "info": "info", "debug": "debug", "trace": "debug"}


def classify(line):
    """Return (level, message) for one log line (JSON or plain text)."""
    stripped = line.strip()
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
            raw = next((obj[k] for k in ("level", "severity", "lvl", "log.level", "@l", "levelname")
                        if k in obj), None)
            msg = next((obj[k] for k in ("message", "msg", "body", "@m", "@mt") if k in obj), stripped)
            if raw is not None:
                level = _WORD_TO_LEVEL.get(str(raw).lower(), None)
                if level is None and str(raw).isdigit():  # pino/bunyan numeric levels
                    n = int(raw)
                    level = "error" if n >= 50 else "warn" if n >= 40 else "info" if n >= 30 else "debug"
                return level or "info", str(msg)
        except ValueError:
            pass
    m = _LEVEL_WORD.search(stripped[:120])
    if m:
        return _WORD_TO_LEVEL[m.group(1).lower()], stripped
    if _ERROR_HINT.search(stripped):
        return "error", stripped
    return "info", stripped


def group_lines(text, min_level="warn", contains=None):
    groups = OrderedDict()
    total = 0
    for raw in text.splitlines():
        if not raw.strip():
            continue
        m = _TS.match(raw)
        ts, line = (m.group(1), m.group(2)) if m else (None, raw)
        if contains and contains.lower() not in line.lower():
            continue
        level, msg = classify(line)
        if LEVELS[level] < LEVELS[min_level]:
            continue
        total += 1
        key = (level, normalise(msg))
        g = groups.get(key)
        if g is None:
            groups[key] = {"level": level, "pattern": key[1], "count": 1, "first_seen": ts,
                           "last_seen": ts, "example": line.strip()[:300]}
        else:
            g["count"] += 1
            g["last_seen"] = ts or g["last_seen"]
    rows = sorted(groups.values(), key=lambda g: (-LEVELS[g["level"]], -g["count"]))
    return total, rows


def search_logs(services, since="5m", level="warn", contains=None):
    if isinstance(services, str):
        services = [s for s in re.split(r"[,\s]+", services) if s]
    services = services[:MAX_SERVICES]
    since_s = parse_window(since)
    level = level if level in LEVELS else "warn"
    out = {"since_s": since_s, "min_level": level, "contains": contains, "services": {}}
    for svc in services:
        try:
            text, log_source = read_logs(svc, since_s, level)
        except SourceError as e:
            out["services"][svc] = {"error": str(e)[:200]}
            continue
        total, rows = group_lines(text, level, contains)
        for r in rows[:8]:
            if r["level"] in ("error", "warn") or contains:
                r["evidence"] = registry.add(
                    TOOL, "log",
                    f"{svc} logs ({log_source}, {since_s}s): {r['count']}x {r['level'].upper()} "
                    f"'{r['example'][:160]}' first {r['first_seen']} last {r['last_seen']}",
                    {"service": svc, "log_source": log_source, **r})
        entry = {"log_source": log_source, "matching_lines": total, "patterns": rows[:TOP_PATTERNS]}
        if not rows:
            entry["evidence"] = registry.add(
                TOOL, "log", f"{svc}: no {level}+ log lines"
                f"{' containing ' + repr(contains) if contains else ''} in last {since_s}s ({log_source})",
                {"service": svc, "since_s": since_s, "level": level, "contains": contains,
                 "log_source": log_source})
        out["services"][svc] = entry
    return cap(_flatten_for_cap(out))


def _level_word(doc):
    n = doc.get("severity_number")
    if isinstance(n, (int, float)) and n:
        return "ERROR" if n >= 17 else "WARN" if n >= 13 else "INFO" if n >= 9 else "DEBUG"
    return (doc.get("severity_text") or "INFO").upper()


def records_to_text(docs):
    """OpenSearch records (newest first) -> 'timestamp LEVEL: body' lines, oldest first."""
    lines = []
    for d in reversed(docs):
        body = d.get("body")
        body = body if isinstance(body, str) else json.dumps(body, default=str)
        lines.append(f"{d.get('ts', '')} {_level_word(d)}: {' '.join(str(body).split())}")
    return "\n".join(lines)


def read_logs(service, since_s, level):
    """(text, log_source): OpenSearch when it has records for the service, else docker logs."""
    src = get_source()
    reader = getattr(src, "opensearch_logs", None)
    checked = []
    if reader:
        try:
            docs = reader(service, since_s, OTEL_MIN_SEVERITY[level])
        except SourceError:
            docs = None
        if docs:
            return records_to_text(docs), "opensearch"
        if docs is not None:
            checked.append("opensearch (none)")
    text = src.docker_logs(service, since_s)
    return text, " + ".join(checked + ["docker logs"])


def _flatten_for_cap(out):
    # cap() trims top-level lists; expose patterns as one list so trimming is fair across services.
    patterns = []
    for svc, entry in out["services"].items():
        for p in entry.get("patterns", []):
            patterns.append({"service": svc, **p})
        entry.pop("patterns", None)
    patterns.sort(key=lambda p: (-LEVELS[p["level"]], -p["count"]))
    out["patterns"] = patterns[:TOP_PATTERNS]
    for p in out["patterns"]:  # pattern text is redundant with the example; keep results small
        p.pop("pattern", None)
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("services", nargs="+")
    p.add_argument("--since", default="5m")
    p.add_argument("--level", default="warn", choices=list(LEVELS))
    p.add_argument("--contains")
    run_cli(search_logs, p, lambda a: {"services": a.services, "since": a.since,
                                       "level": a.level, "contains": a.contains})


if __name__ == "__main__":
    main()
