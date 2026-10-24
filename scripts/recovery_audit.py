"""Recovery audit: inject every faultctl fault, reset it, and check that everything recovers.

For each fault: inject -> confirm the effect -> wait -> faultctl reset -> check recovery at three
levels, timed from the reset:
  container   running and healthy, CPU/memory limits equivalent to before, no restart loop left
  dependents  the shop's public APIs (which exercise every caller of the target) answer 200
  telemetry   detector.py's span metrics: target and its callers back near the healthy snapshot

    python scripts/recovery_audit.py [--only cache-down stop-payment ...] [--hold 60]

Results go to runs/recovery_audit-<timestamp>.json and a table is printed at the end.
This is test-harness code like faultctl: the agent never imports or reads it.
"""
import argparse
import json
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import detector  # noqa: E402
import faultctl  # noqa: E402
from faultctl import core  # noqa: E402

# The organisers' tool is also called faultctl: never drive theirs by accident.
if Path(faultctl.__file__).resolve().parent != ROOT / "faultctl":
    sys.exit(f"refusing to run: 'faultctl' resolved to {faultctl.__file__}, not this repo's harness")
from oncall import config  # noqa: E402

SHOP = "http://localhost:8080"
PROJECT = "opentelemetry-demo"
RPS_RECOVERED = 0.5      # target/callers back to >= 50% of healthy rps
ERR_MARGIN = 0.02        # and error rate <= healthy + 2 points
SHOPPER = {"email": "audit@example.com", "userCurrency": "USD",
           "address": {"streetAddress": "1600 Amphitheatre Parkway", "zipCode": "94043",
                       "city": "Mountain View", "state": "CA", "country": "United States"},
           "creditCard": {"creditCardNumber": "4432-8015-6152-0454", "creditCardExpirationMonth": 1,
                          "creditCardExpirationYear": 2039, "creditCardCvv": 672}}


def log(msg):
    print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)


# --- observations -------------------------------------------------------------------------

def containers():
    """{name: {status, health, restarts, nano_cpus, memory}} for every demo container."""
    names = subprocess.run(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={PROJECT}",
                            "--format", "{{.Names}}"], capture_output=True, text=True).stdout.split()
    out = {}
    for info in json.loads(subprocess.run(["docker", "inspect", *names], capture_output=True, text=True).stdout or "[]"):
        st, hc = info["State"], info["HostConfig"]
        out[info["Name"].lstrip("/")] = {
            "status": st["Status"], "health": (st.get("Health") or {}).get("Status", "none"),
            "restarts": info["RestartCount"], "nano_cpus": hc.get("NanoCpus", 0), "memory": hc.get("Memory", 0)}
    return out


def shop_checks():
    """Public shop calls that between them reach every service; {name: status code or error}."""
    s, user = requests.Session(), str(uuid.uuid4())
    calls = {
        "home": ("GET", "/", {}),
        "products": ("GET", "/api/products", {"params": {"currencyCode": "USD"}}),
        "product": ("GET", "/api/products/OLJCESPC7Z", {"params": {"currencyCode": "USD"}}),
        "recommendations": ("GET", "/api/recommendations", {"params": {"productIds": ["OLJCESPC7Z"], "sessionId": user}}),
        "ads": ("GET", "/api/data/", {"params": {"contextKeys": ["telescopes"]}}),
        "cart_add": ("POST", "/api/cart", {"json": {"item": {"productId": "OLJCESPC7Z", "quantity": 1}, "userId": user}}),
        "cart_get": ("GET", "/api/cart", {"params": {"sessionId": user, "currencyCode": "USD"}}),
        "checkout": ("POST", "/api/checkout", {"params": {"currencyCode": "USD"}, "json": {**SHOPPER, "userId": user}}),
    }
    out = {}
    for name, (method, path, kw) in calls.items():
        try:
            out[name] = s.request(method, SHOP + path, timeout=20, **kw).status_code
        except requests.RequestException as e:
            out[name] = type(e).__name__
    return out


def telemetry():
    snap = detector.snapshot()
    return {svc: {"rps": snap["rps"].get(svc, 0.0), "err": snap["error_rate"].get(svc, 0.0)}
            for svc in set(snap["rps"]) | set(snap["error_rate"])}


def callers_of(target):
    return sorted(c for c, callees in config.DEPENDENCIES.items() if target in callees)


def loop_alive(pid):
    if not pid:
        return False
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    return str(pid) in out


# --- checks -------------------------------------------------------------------------------

def same_cpu(before, now, host_cpus):
    # docker cannot clear a CPU limit; "no limit" and "all host CPUs" are equivalent.
    unlimited = {0, host_cpus * 10 ** 9}
    return now == before or (before in unlimited and now in unlimited)


def container_ok(target, healthy, host_cpus):
    c = containers().get(target, {})
    h = healthy[target]
    problems = []
    if c.get("status") != "running":
        problems.append(f"status {c.get('status')}")
    if c.get("health") not in ("healthy", "none"):
        problems.append(f"health {c.get('health')}")
    if not same_cpu(h["nano_cpus"], c.get("nano_cpus"), host_cpus):
        problems.append(f"cpus {c.get('nano_cpus') / 1e9:g} (was {h['nano_cpus'] / 1e9:g})")
    if c.get("memory") != h["memory"]:
        problems.append(f"memory {c.get('memory')} (was {h['memory']})")
    return problems, c


def telemetry_ok(services, healthy_tel):
    now = telemetry()
    problems = []
    for svc in services:
        h = healthy_tel.get(svc)
        if not h or h["rps"] < detector.MIN_RPS:
            continue  # no healthy traffic to compare (e.g. valkey-cart has no spans)
        n = now.get(svc, {"rps": 0.0, "err": 0.0})
        if n["rps"] < RPS_RECOVERED * h["rps"]:
            problems.append(f"{svc} rps {n['rps']:.3f} < {RPS_RECOVERED:.0%} of {h['rps']:.3f}")
        if n["err"] > h["err"] + ERR_MARGIN:
            problems.append(f"{svc} errors {n['err']:.1%} (healthy {h['err']:.1%})")
    return problems


def wait_until(check, timeout, every=10):
    """Run check() until it returns no problems; (seconds taken or None, last problems)."""
    start = time.time()
    while True:
        problems = check()
        if not problems:
            return round(time.time() - start), []
        if time.time() - start >= timeout:
            return None, problems
        time.sleep(every)


# --- one fault ----------------------------------------------------------------------------

def audit_fault(fault, hold, healthy, healthy_tel, host_cpus):
    target, kind = fault.container, fault.kind
    rec = {"fault": fault.id, "target": target, "kind": kind}
    before = containers()
    log(f"== {fault.id}: inject")
    rec["inject"] = faultctl.inject(fault.id)
    pid = core.load_state()["faults"].get(fault.id, {}).get("pid")

    time.sleep(25 if kind == "restart_loop" else 5)
    c = containers()[target]
    effect = {"stop": c["status"] != "running",
              "cpu_starve": c["nano_cpus"] == int(core.CPU_LIMIT * 1e9),
              "mem_starve": c["memory"] < before[target]["memory"],
              "restart_loop": c["restarts"] > before[target]["restarts"] or c["status"] != "running"}[kind]
    rec["effect"] = {"confirmed": effect, "status": c["status"], "restarts": c["restarts"],
                     "nano_cpus": c["nano_cpus"], "memory": c["memory"]}
    log(f"   effect confirmed={effect} {rec['effect']}")
    time.sleep(max(0, hold - (25 if kind == "restart_loop" else 5)))

    log("   reset")
    t0 = time.time()
    rec["reset"] = faultctl.reset(everything=False)

    t, problems = wait_until(lambda: container_ok(target, healthy, host_cpus)[0], 120, every=5)
    settled = containers()[target]["restarts"]
    time.sleep(25 if kind == "restart_loop" else 0)  # a leftover loop would have crashed it again
    leftover = loop_alive(pid) or bool(core.load_state()["faults"]) or \
        containers()[target]["restarts"] > settled
    if leftover:
        problems.append("restart loop still running (process alive, state left, or restarts climbing)")
    rec["container"] = {"ok": t is not None and not leftover, "seconds": t, "problems": problems}

    t2, shop_problems = wait_until(lambda: [f"{k}={v}" for k, v in shop_checks().items() if v != 200], 180)
    rec["dependents"] = {"ok": t2 is not None, "seconds": t2, "callers": callers_of(target),
                         "problems": shop_problems,
                         "callers_running": {c: containers().get(c, {}).get("status") for c in callers_of(target)}}

    watch = [target] + callers_of(target)
    t3, tel_problems = wait_until(lambda: telemetry_ok(watch, healthy_tel), 330, every=20)
    rec["telemetry"] = {"ok": t3 is not None, "seconds": t3, "watched": watch, "problems": tel_problems}
    rec["recover_seconds"] = round(time.time() - t0) if t3 is not None else None
    log(f"   container={rec['container']['ok']} dependents={rec['dependents']['ok']} "
        f"telemetry={rec['telemetry']['ok']} in {rec['recover_seconds']}s {tel_problems or ''}")
    return rec


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--only", nargs="*", help="fault ids to audit (default: all)")
    p.add_argument("--hold", type=int, default=60, help="seconds a fault stays injected")
    args = p.parse_args()
    faults = [f for f in core.FAULTS.values() if not args.only or f.id in args.only]

    print("\n".join(faultctl.reset(everything=False)))
    host_cpus = core.host_cpus()
    healthy = containers()
    healthy_tel = telemetry()
    shop = shop_checks()
    log(f"healthy snapshot: {len(healthy)} containers, shop {shop}")
    report = {"started": datetime.now().isoformat(timespec="seconds"), "healthy_containers": healthy,
              "healthy_telemetry": healthy_tel, "healthy_shop": shop, "faults": []}
    out = config.RUNS_DIR / f"recovery_audit-{datetime.now():%Y%m%d-%H%M%S}.json"
    try:
        for f in faults:
            report["faults"].append(audit_fault(f, args.hold, healthy, healthy_tel, host_cpus))
            out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    finally:
        print("\n".join(faultctl.reset(everything=False)))
        end = containers()
        expected = {f.container for f in faults if f.kind in ("restart_loop", "mem_starve", "stop")}
        report["side_effects"] = {
            name: {"restarts": f"{healthy.get(name, {}).get('restarts')} -> {c['restarts']}",
                   "status": c["status"], "health": c["health"], "expected": name in expected}
            for name, c in end.items()
            if c["restarts"] != healthy.get(name, {}).get("restarts") or c["status"] != "running"
            or c["health"] not in ("healthy", "none")}
        report["finished"] = datetime.now().isoformat(timespec="seconds")
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        log(f"saved {out}")

    print(f"\n{'fault':30} {'target':16} {'container':10} {'dependents':11} {'telemetry':10} {'recover':>8}")
    for r in report["faults"]:
        mark = lambda x: "yes" if x["ok"] else "NO"  # noqa: E731
        print(f"{r['fault']:30} {r['target']:16} {mark(r['container']):10} {mark(r['dependents']):11} "
              f"{mark(r['telemetry']):10} {str(r['recover_seconds']) + 's' if r['recover_seconds'] else '-':>8}")
    print("side effects:", json.dumps(report["side_effects"], indent=1))


if __name__ == "__main__":
    main()
