"""python -m faultctl list | inject <id> | reset [--all] | status"""
import argparse
import sys

from faultctl import core


def main():
    p = argparse.ArgumentParser(prog="faultctl", description="Inject and reset practice faults.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="every fault with a one-line description")
    inj = sub.add_parser("inject", help="inject one fault")
    inj.add_argument("fault", choices=sorted(core.FAULTS), metavar="FAULT")
    r = sub.add_parser("reset", help="undo the faults faultctl recorded (safe to repeat, works after a crash)")
    r.add_argument("--all", action="store_true", dest="everything",
                   help="also kill leftover loops, restore starved limits and start every stopped "
                        "demo container, even ones faultctl did not touch")
    sub.add_parser("status", help="what is injected right now")
    loop = sub.add_parser("_loop")  # internal: background process for restart-loop faults
    loop.add_argument("fault")
    args = p.parse_args()

    try:
        if args.cmd == "list":
            lines = core.list_faults()
        elif args.cmd == "inject":
            lines = [core.inject(args.fault)]
        elif args.cmd == "reset":
            lines = core.reset(everything=args.everything)
        elif args.cmd == "status":
            lines = core.status()
        else:
            core.run_loop(args.fault)
            return 0
    except (core.DockerError, RuntimeError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
