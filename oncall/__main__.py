"""Incident Zero CLI.

  python -m oncall check                         verify endpoints (read-only)
  python -m oncall baseline --window 5m          save healthy numbers (before injecting faults)
  python -m oncall investigate --service checkout --signal error_rate --value 0.3
  python -m oncall investigate --alert-file alert.json
  python -m oncall --test investigate --alert-file tests/fixtures/payment_down/alert.json
  python -m oncall watch                         threshold watcher -> investigation
  python -m oncall stop | resume                 kill switch on / off

Source flags (before the command). Default is live. --source bundle --bundle DIR replays a recording;
--record DIR saves one while live; --test [--fixtures DIR] uses fixtures under tests/fixtures/ and
is the ONLY way to run on fixture data. Every report records the source used.
"""
import argparse
import json
import sys
from pathlib import Path

from oncall import config
from oncall.sources import SourceError, add_source_args, apply_source_args, get_source, source_info


def cmd_check(_args):
    """Read-only connectivity check for Prometheus, Jaeger and docker."""
    from oncall.tools._common import metric_names
    src = get_source()
    report = source_info()
    try:
        names = src.prom_names()
        found = metric_names()
        report["prometheus"] = {"metric_names": len(names), "span_metrics": found}
        if found.get("calls"):
            sample = src.prom_query(f"topk(1, {found['calls']})")
            report["prometheus"]["calls_labels"] = sorted(sample[0]["metric"]) if sample else []
        keys = ("cpu", "memory", "gc", "jvm", "runtime", "container", "process")
        report["prometheus"]["runtime_metrics"] = sorted(
            n for n in names if any(k in n.lower() for k in keys))[:80]
    except (SourceError, KeyError, IndexError) as e:
        report["prometheus"] = {"error": str(e)[:300]}
    try:
        report["jaeger"] = {"base": getattr(src, "jaeger_base", lambda: "bundle")(),
                            "services": sorted(src.jaeger_services())}
    except SourceError as e:
        report["jaeger"] = {"error": str(e)[:300]}
    try:
        names = src.container_names()
        report["docker"] = {"containers": names}
        if names:
            i = src.docker_inspect([names[0]])[0]
            report["docker"]["inspect_sample"] = {
                "name": i["Name"], "Status": i["State"]["Status"],
                "OOMKilled": i["State"]["OOMKilled"], "RestartCount": i["RestartCount"],
                "Health": (i["State"].get("Health") or {}).get("Status", "no healthcheck")}
    except SourceError as e:
        report["docker"] = {"error": str(e)[:300]}
    print(json.dumps(report, indent=2))


def cmd_baseline(args):
    from oncall.tools.metrics import save_baseline
    try:
        print(json.dumps(save_baseline(args.window), indent=2))
    except RuntimeError as e:
        sys.exit(str(e))


def _alert_from_args(args):
    if args.alert_file:
        return json.loads(Path(args.alert_file).read_text())
    if not args.service:
        sys.exit("give --service (and optionally --signal/--value) or --alert-file")
    return {"source": "manual", "service": args.service, "signal": args.signal, "value": args.value}


def cmd_investigate(args):
    from oncall.agent.run import run_agent
    record = run_agent(_alert_from_args(args), model=args.model, max_steps=args.max_steps)
    if args.json:
        print(json.dumps(record["summary"], indent=2, default=str))


def cmd_watch(args):
    from oncall.agent.run import run_agent
    from oncall.watcher import watch
    watch(lambda alert: run_agent(alert), interval=args.interval, window=args.window,
          once=args.once)


def cmd_stop(_args):
    config.KILL_SWITCH_FILE.write_text("stop\n")
    print(f"kill switch ON ({config.KILL_SWITCH_FILE})")


def cmd_resume(_args):
    config.KILL_SWITCH_FILE.unlink(missing_ok=True)
    print("kill switch OFF")


def main():
    p = argparse.ArgumentParser(prog="oncall", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    add_source_args(p)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    b = sub.add_parser("baseline")
    b.add_argument("--window", default="5m")
    inv = sub.add_parser("investigate")
    inv.add_argument("--service")
    inv.add_argument("--signal", default="error_rate")
    inv.add_argument("--value")
    inv.add_argument("--alert-file")
    inv.add_argument("--model")
    inv.add_argument("--max-steps", type=int)
    inv.add_argument("--json", action="store_true", help="print the summary JSON at the end")
    w = sub.add_parser("watch")
    w.add_argument("--interval", type=int, default=15)
    w.add_argument("--window", default="2m")
    w.add_argument("--once", action="store_true", help="exit after the first investigation")
    sub.add_parser("stop")
    sub.add_parser("resume")
    args = p.parse_args()
    if args.cmd not in ("stop", "resume"):
        apply_source_args(p, args)
    {"check": cmd_check, "baseline": cmd_baseline, "investigate": cmd_investigate,
     "watch": cmd_watch, "stop": cmd_stop, "resume": cmd_resume}[args.cmd](args)


if __name__ == "__main__":
    main()
