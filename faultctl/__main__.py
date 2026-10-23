"""python -m faultctl list | inject <id> | reset | status"""
import argparse
import sys

from faultctl import core


def main():
    p = argparse.ArgumentParser(prog="faultctl", description="Inject and reset practice faults.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="every fault with a one-line description")
    inj = sub.add_parser("inject", help="inject one fault")
    inj.add_argument("fault", choices=sorted(core.FAULTS), metavar="FAULT")
    sub.add_parser("reset", help="undo everything (safe to repeat, works after a crash)")
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
            lines = core.reset()
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
