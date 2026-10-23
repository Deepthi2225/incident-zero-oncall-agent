"""Helpers shared by all tools: window parsing, result capping, metric discovery, CLI wrapper."""
import argparse
import json
import re

from oncall import config
from oncall.evidence import registry
from oncall.sources import get_source, set_source

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_window(value, default=300):
    """'5m' -> 300, '90s' -> 90, 120 -> 120."""
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return int(value)
    m = re.fullmatch(r"\s*(\d+)\s*([smhd]?)\s*", str(value))
    if not m:
        raise ValueError(f"bad window {value!r}; use e.g. 90s, 5m, 1h")
    return int(m.group(1)) * _UNITS[m.group(2) or "s"]


def prom_window(seconds):
    """Prometheus range selector, never shorter than 1m so rate() has enough samples."""
    return f"{max(int(seconds), 60)}s"


def cap(result, max_bytes=None):
    """Shrink the longest list in result until its JSON fits max_bytes. Marks truncation."""
    max_bytes = max_bytes or config.TOOL_RESULT_MAX_BYTES
    while len(json.dumps(result, default=str)) > max_bytes:
        lists = [(k, v) for k, v in result.items() if isinstance(v, list) and len(v) > 1]
        if not lists:
            break
        key, longest = max(lists, key=lambda kv: len(json.dumps(kv[1], default=str)))
        result[key] = longest[:-1]
        result["truncated"] = True
    return result


_metric_names = {}


def metric_names():
    """Discover span-metric names once per process (they differ between demo versions)."""
    if _metric_names:
        return _metric_names
    names = set(get_source().prom_names())
    for kind, candidates in (("calls", config.CALLS_METRIC_CANDIDATES),
                             ("duration", config.DURATION_METRIC_CANDIDATES)):
        found = next((c for c in candidates if c in names), None)
        if not found:  # fall back to anything that looks right
            suffix = "calls_total" if kind == "calls" else "_bucket"
            found = next((n for n in sorted(names)
                          if "span" in n and n.endswith(suffix)), None)
        _metric_names[kind] = found
    return _metric_names


_NORMALISERS = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{12,}\b", re.I), "<hex>"),
    (re.compile(r"\d+(?:\.\d+)?"), "<n>"),
]


def normalise(text, max_len=200):
    """Collapse ids, addresses and numbers so similar messages group together."""
    text = " ".join(str(text).split())
    for pattern, repl in _NORMALISERS:
        text = pattern.sub(repl, text)
    return text[:max_len]


def parse_docker_time(value):
    """'2026-10-23T07:10:00.123456789Z' -> epoch seconds (None for docker's zero time)."""
    from datetime import datetime, timezone
    if not value or value.startswith("0001-"):
        return None
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?", value)
    if not m:
        return None
    dt = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
    tz = m.group(3) or "Z"
    if tz == "Z":
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = datetime.fromisoformat(m.group(1) + tz)
    frac = float("0." + m.group(2)) if m.group(2) else 0.0
    return dt.timestamp() + frac


def fmt_num(x, digits=3):
    if x is None:
        return None
    return round(float(x), digits)


def run_cli(fn, parser: argparse.ArgumentParser, to_kwargs):
    """Standard CLI entry: --source live|bundle, --bundle DIR, --record DIR; prints result + evidence."""
    parser.add_argument("--source", choices=["live", "bundle"], default=config.SOURCE)
    parser.add_argument("--bundle", help="bundle directory for --source bundle")
    parser.add_argument("--record", help="save live answers into this bundle directory")
    parser.add_argument("--evidence", action="store_true", help="also print registered evidence")
    args = parser.parse_args()
    set_source(args.source, bundle_dir=args.bundle, record_to=args.record)
    result = fn(**to_kwargs(args))
    print(json.dumps(result, indent=2, default=str))
    if args.evidence:
        print(json.dumps(registry.dump(), indent=2))
