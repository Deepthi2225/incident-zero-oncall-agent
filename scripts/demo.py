"""End-to-end demo: inject a fault -> wait -> run the agent -> print the summary -> reset.

    python scripts/demo.py stop-payment [--wait 90] [--save-baseline]

The agent only receives an alert built from live telemetry by the threshold watcher; it is never
told which fault was injected. The fault is always reset at the end, even on Ctrl-C or errors.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import incident  # noqa: E402
from oncall import config  # noqa: E402
from oncall.agent.run import run_agent  # noqa: E402
from oncall.sources import set_source  # noqa: E402
from oncall.tools.metrics import compute_health, load_baseline, save_baseline  # noqa: E402
from oncall.watcher import breaches, make_alert  # noqa: E402


def build_alert():
    """Alert from the watcher's thresholds; generic entry-point alert if metrics have not caught up."""
    try:
        health = compute_health("2m")
        found = breaches(health, load_baseline())
        if found:
            return make_alert(found, health["window_s"])
    except Exception as e:  # metrics unavailable should not stop the demo
        print(f"(watcher check failed: {e})")
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
    p.add_argument("fault", choices=sorted(incident.FAULTS))
    p.add_argument("--wait", type=int, default=90, help="seconds between inject and agent run")
    p.add_argument("--save-baseline", action="store_true",
                   help="capture the healthy baseline before injecting")
    p.add_argument("--max-steps", type=int)
    args = p.parse_args()

    set_source("live")
    if args.save_baseline:
        print(f"baseline: {save_baseline('5m')}")
    elif not config.BASELINE_FILE.exists():
        print("warning: no baseline saved; run `python -m oncall baseline` while healthy")

    record = None
    try:
        print(f"== inject {args.fault}")
        incident.inject(args.fault)
        wait(args.wait)
        alert = build_alert()
        print(f"== alert: {json.dumps(alert)}")
        record = run_agent(alert, max_steps=args.max_steps)
    finally:
        print("== reset")
        incident.reset()

    print("\n== summary JSON")
    print(json.dumps(record["summary"], indent=2, default=str))
    print(f"run saved to {record['path']} (outcome: {record['outcome']})")


if __name__ == "__main__":
    main()
