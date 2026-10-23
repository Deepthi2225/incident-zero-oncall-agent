"""Practice-incident injector for our own testing. Uses only `docker stop` / `docker start`.

    python scripts/incident.py inject stop-payment|stop-cart|stop-valkey
    python scripts/incident.py reset          start any of these containers that are stopped
    python scripts/incident.py status

The agent must never import or read this file (it would know the answer); a test enforces that.
Organiser incidents use faultctl instead.
"""
import argparse
import subprocess
import sys

FAULTS = {
    "stop-payment": "payment",
    "stop-cart": "cart",
    "stop-valkey": "valkey-cart",
}


def docker(*args):
    out = subprocess.run(["docker", *args], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"docker {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout.strip()


def state(container):
    return docker("inspect", "--format", "{{.State.Status}}", container)


def inject(fault):
    container = FAULTS[fault]
    docker("stop", container)
    print(f"injected {fault}: {container} is {state(container)}")


def reset():
    for container in FAULTS.values():
        before = state(container)
        if before != "running":
            docker("start", container)
        print(f"{container}: {before} -> {state(container)}")


def status():
    for container in FAULTS.values():
        print(f"{container}: {state(container)}")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    inj = sub.add_parser("inject")
    inj.add_argument("fault", choices=sorted(FAULTS))
    sub.add_parser("reset")
    sub.add_parser("status")
    args = p.parse_args()
    if args.cmd == "inject":
        inject(args.fault)
    elif args.cmd == "reset":
        reset()
    else:
        status()


if __name__ == "__main__":
    sys.exit(main())
