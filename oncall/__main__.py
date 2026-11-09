"""P0ppy CLI.

  python -m oncall check                         verify endpoints (read-only)
  python -m oncall baseline --window 5m          save healthy numbers (before injecting faults)
  python -m oncall investigate --service checkout --signal error_rate --value 0.3
  python -m oncall investigate --alert-file alert.json
  python -m oncall --test investigate --alert-file tests/fixtures/payment_down/alert.json
  python -m oncall watch                         detector.py -> investigation
  python -m oncall verify <run_id>               verify-only, after a human fixed it by hand
  python -m oncall stop | resume                 kill switch on / off
  python -m oncall emailtest                     send one test email with the .env settings
  python -m oncall calltest                      place one test phone call with the .env settings
  python -m oncall telegramtest                  send one test Telegram message with the .env settings
  python -m oncall slacktest                     send one test Slack message with the .env settings
  python -m oncall ack [incident]                acknowledge an incident, or list those waiting
  python -m oncall whoison                       show who each rotation has on call today

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
        return json.loads(Path(args.alert_file).read_text(encoding="utf-8"))
    if not args.service:
        sys.exit("give --service (and optionally --signal/--value) or --alert-file")
    return {"source": "manual", "service": args.service, "signal": args.signal, "value": args.value}


def cmd_investigate(args):
    from oncall.agent.run import run_agent
    record = run_agent(_alert_from_args(args), model=args.model, max_steps=args.max_steps)
    if args.json:
        print(json.dumps({k: v for k, v in record.items() if k != "path"}, indent=2, default=str))


def cmd_watch(args):
    # One source of truth for detection: detector.py and its calibrated per-service limits.
    sys.path.insert(0, str(config.ROOT))
    import detector
    from oncall import ack
    from oncall.agent.run import run_agent
    detector.POLL_SECS = args.interval
    detector.run(on_incident=run_agent, once=args.once, on_tick=ack.escalate_overdue)


def find_run(run):
    """A run id (20261023-175355), its -test/-bundle variant, or a path to the run record."""
    path = Path(run)
    if path.suffix == ".json" and path.exists():
        return path
    for candidate in sorted(config.RUNS_DIR.glob(f"{run}*.json")):
        if candidate.name != "index.json":
            return candidate
    sys.exit(f"no run record for {run!r} in {config.RUNS_DIR}")


def cmd_verify(args):
    """Verify-only: after a human fixed the issue by hand. Reads telemetry, never calls the Action API."""
    from oncall.evidence import registry
    from oncall.record import load, save_existing
    from oncall.remediate import verify_manual
    from oncall.verify import Verifier
    path = find_run(args.run)
    record = load(path)  # upgrades a Day 1 record to the current format
    registry.load(record.get("evidence", []))  # new checks continue the run's evidence IDs
    verifier = Verifier(timeout_s=args.timeout) if args.timeout else Verifier()
    diag = {"root_causes": (record.get("diagnosis") or {}).get("root_causes") or [],
            "remediation_plan": record.get("remediation_plan") or {}}
    result = verify_manual(diag, verifier, record["metadata"]["run_id"])
    record.setdefault("manual_verifications", []).append(result)
    saved = save_existing(record, path)
    print(f"manual verification: {result['outcome'].upper()} | outcome {saved['outcome']['status']} | "
          f"run updated {path} | report {path.with_suffix('.report.md')}")


def cmd_stop(_args):
    config.KILL_SWITCH_FILE.write_text("stop\n")
    print(f"kill switch ON ({config.KILL_SWITCH_FILE})")


def cmd_emailtest(_args):
    """One clearly labelled test email with the app's own settings. Does not run the agent."""
    from oncall import notify
    print("email settings: " + " | ".join(notify.settings_lines()))
    entry = notify.send_test()
    if entry["status"] == "sent":
        via = f" (resend id {entry['message_id']})" if entry.get("message_id") else ""
        print(f"sent{via} to {', '.join(entry['recipients'])}")
        return
    print(entry.get("error") or "email failed: unknown reason")
    sys.exit(1)


def cmd_calltest(_args):
    """One clearly labelled test call. Does not run the agent."""
    from oncall import notify
    entry = notify.send_test_call()
    if entry["status"] == "sent":
        print(f"call placed to {', '.join(entry['recipients'])} (twilio id {entry['message_id']})")
        return
    print(entry.get("error") or "call failed: unknown reason")
    sys.exit(1)


def cmd_telegramtest(_args):
    """One clearly labelled test message, or the chat id to use when it is not set yet."""
    from oncall import config, notify
    if config.TELEGRAM_BOT_TOKEN and not config.TELEGRAM_CHAT_ID:
        chats = notify.telegram_chats()
        if not chats:
            print("no chats found: send your bot any message in Telegram, check TELEGRAM_BOT_TOKEN, and run this again")
            sys.exit(1)
        for chat_id, name in chats:
            print(f"found chat with {name}: add TELEGRAM_CHAT_ID={chat_id} to .env")
        return
    entry = notify.send_test_telegram()
    if entry["status"] == "sent":
        print(f"telegram message sent to chat {', '.join(entry['recipients'])}")
        return
    print(entry.get("error") or "telegram failed: unknown reason")
    sys.exit(1)


def cmd_slacktest(_args):
    """One clearly labelled test message. Does not run the agent."""
    from oncall import notify
    entry = notify.send_test_slack()
    print("slack message sent" if entry["status"] == "sent" else entry.get("error"))
    if entry["status"] != "sent":
        sys.exit(1)


def cmd_ack(args):
    from oncall import ack
    if args.incident:
        ack.acknowledge(args.incident)
        print(f"acknowledged {args.incident}")
        return
    print("\n".join(f"{i}: {row.get('subject')}" for i, row in ack.waiting().items()) or "nothing waiting")


def cmd_whoison(_args):
    from oncall import notify
    rotations = notify._escalation().get("rotations") or {}
    print("\n".join(f"{name}: {notify.on_call(name) or 'cannot tell, check escalation.json'}" for name in rotations)
          or "no rotations in escalation.json")


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
    w.add_argument("--interval", type=int, default=30, help="seconds between polls")
    w.add_argument("--once", action="store_true", help="exit after the first investigation")
    v = sub.add_parser("verify", help="verify an existing run after a human fixed it by hand")
    v.add_argument("run", help="run id (e.g. 20261024-101500) or path to runs/<id>.json")
    v.add_argument("--timeout", type=float, help="seconds to keep checking (default VERIFY_TIMEOUT_S)")
    sub.add_parser("stop")
    sub.add_parser("resume")
    sub.add_parser("emailtest", help="send one test email with the email settings in .env")
    sub.add_parser("calltest", help="place one test phone call with the Twilio settings in .env")
    sub.add_parser("telegramtest", help="send one test Telegram message with the settings in .env")
    sub.add_parser("slacktest", help="send one test Slack message with the settings in .env")
    a = sub.add_parser("ack", help="acknowledge an incident, or list the ones waiting")
    a.add_argument("incident", nargs="?")
    sub.add_parser("whoison", help="show who each rotation has on call today")
    args = p.parse_args()
    if args.cmd not in ("stop", "resume", "emailtest", "calltest", "telegramtest", "slacktest", "ack", "whoison"):
        apply_source_args(p, args)
    {"check": cmd_check, "baseline": cmd_baseline, "investigate": cmd_investigate,
     "watch": cmd_watch, "verify": cmd_verify, "stop": cmd_stop, "resume": cmd_resume,
     "emailtest": cmd_emailtest, "calltest": cmd_calltest, "telegramtest": cmd_telegramtest,
     "slacktest": cmd_slacktest, "ack": cmd_ack, "whoison": cmd_whoison}[args.cmd](args)


if __name__ == "__main__":
    main()
