"""End-to-end demo: inject a fault -> wait -> run the agent -> print the summary -> reset.

    python scripts/demo.py stop-payment [--wait 180] [--save-baseline]
    python scripts/demo.py stop-payment --test     rehearsal on fixtures: no docker, no injection

Live by default. The agent only receives an alert that detector.py (the one detector, with its
calibrated per-service limits) builds from telemetry; it is never told which fault was injected. The fault is always reset at the end, even on Ctrl-C
or errors. The summary records which source was used.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import detector  # noqa: E402
import faultctl  # noqa: E402
from oncall.agent.run import run_agent  # noqa: E402
from oncall.sources import set_source, source_info  # noqa: E402
from oncall.tools.metrics import load_baseline, save_baseline  # noqa: E402


def build_alert():
    """Incident from detector.py's calibrated limits; generic entry-point alert if nothing
    breaches yet (metrics lag) or Prometheus is unreachable."""
    if not detector.load_baseline():
        print("warning: no detector baseline; run `python detector.py --calibrate 300` while healthy")
    try:
        incident = detector.detect_once()
        if incident:
            return incident
    except Exception as e:  # metrics unavailable should not stop the demo
        print(f"(detector check failed: {e})")
    return {"source": "demo", "fired_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "service": "frontend-proxy", "signal": "user_reports",
            "note": "no metric threshold breached yet (metrics arrive about every 60 s); "
                    "users report errors in the shop"}


def wait(seconds):
    end = time.time() + seconds
    while (left := end - time.time()) > 0:
        print(f"  waiting for telemetry... {int(left)}s", flush=True)
        time.sleep(min(15, left))


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("fault", choices=sorted(faultctl.FAULTS), metavar="FAULT",
                   help="a fault id from `python -m faultctl list`")
    p.add_argument("--wait", type=int, default=180,
                   help="seconds between inject and agent run (metrics lag ~1-2 min)")
    p.add_argument("--save-baseline", action="store_true",
                   help="capture the healthy baseline before injecting")
    p.add_argument("--max-steps", type=int)
    p.add_argument("--test", action="store_true",
                   help="rehearse on fixtures under tests/fixtures/ (no docker, nothing injected)")
    p.add_argument("--fixtures", help="fixture directory under tests/fixtures/ (needs --test)")
    args = p.parse_args()
    if args.fixtures and not args.test:
        p.error("--fixtures needs --test")
    if args.test and args.save_baseline:
        p.error("--save-baseline is live only")

    if args.test:
        set_source("test", fixture_dir=args.fixtures, allow_test=True)
        print(f"!!! TEST MODE ({source_info()['source_detail']}): nothing is injected; "
              f"the agent reads fixtures, not the demo")
        # detector.py reads live Prometheus, so test mode uses the scenario's own alert.
        fixture_dir = Path(source_info()["source_detail"].removeprefix("fixtures "))
        alert = json.loads((HERE.parent / fixture_dir / "alert.json").read_text())
        record = run_agent(alert, max_steps=args.max_steps)
    else:
        set_source("live")
        if args.save_baseline:
            print(f"baseline: {save_baseline('5m')}")
        elif load_baseline() is None:
            print("warning: no live baseline saved; run `python -m oncall baseline` while healthy")
        record = None
        try:
            print(f"== inject {args.fault}")
            print(faultctl.inject(args.fault))
            wait(args.wait)
            alert = build_alert()
            print(f"== alert: {json.dumps(alert)}")
            record = run_agent(alert, max_steps=args.max_steps)
        finally:
            print("== reset")
            print("\n".join(faultctl.reset()))

    print("\n== summary JSON")
    print(json.dumps(record["summary"], indent=2, default=str))
    print(f"run saved to {record['path']} (outcome: {record['outcome']}, "
          f"source: {record['source']} - {record['source_detail']})")
    return 0 if record["outcome"] == "submitted" else 1


if __name__ == "__main__":
    sys.exit(main())
