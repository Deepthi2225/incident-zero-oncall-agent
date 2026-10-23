"""Fault catalogue, inject and reset.

Kinds:
    stop          docker stop <container>
    restart_loop  background process that crashes the container every 20 s (SIGTERM to PID 1
                  from inside, so the restart policy restarts it and RestartCount climbs;
                  images without a usable runtime fall back to docker kill + docker start)
    cpu_starve    docker update --cpus 0.1
    mem_starve    docker update --memory to about half the current usage (min 6 MiB)
"""
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_FILE = Path(os.getenv("FAULTCTL_STATE", HERE / "state.json"))
SERVICES = ["payment", "cart", "checkout", "product-catalog", "recommendation", "ad"]
LOOP_INTERVAL = 20
CPU_LIMIT = 0.1
MEM_FLOOR = 6 * 1024 * 1024


@dataclass(frozen=True)
class Fault:
    id: str
    kind: str
    container: str
    description: str


def _catalogue():
    faults = []
    for svc in SERVICES:
        faults.append(Fault(f"stop-{svc}", "stop", svc, f"docker stop {svc} (service down)"))
    faults.append(Fault("cache-down", "stop", "valkey-cart", "docker stop valkey-cart (cart's cache down)"))
    faults.append(Fault("db-down", "stop", "astronomy-db", "docker stop astronomy-db (product-catalog's database down)"))
    for svc in SERVICES:
        faults.append(Fault(f"restart-loop-{svc}", "restart_loop", svc,
                            f"crash {svc} every {LOOP_INTERVAL}s so it keeps restarting"))
    for svc in SERVICES:
        faults.append(Fault(f"cpu-starve-{svc}", "cpu_starve", svc, f"limit {svc} to {CPU_LIMIT} CPU"))
    for svc in SERVICES:
        faults.append(Fault(f"mem-starve-{svc}", "mem_starve", svc,
                            f"cut {svc}'s memory limit to ~half its current usage"))
    return {f.id: f for f in faults}


FAULTS = _catalogue()


# --- docker -----------------------------------------------------------------------

class DockerError(RuntimeError):
    pass


def docker(*args, check=True):
    env = {**os.environ, "MSYS_NO_PATHCONV": "1"}  # keep "/" args intact under Git Bash
    out = subprocess.run(["docker", *args], capture_output=True, text=True, env=env)
    if check and out.returncode != 0:
        raise DockerError(f"docker {' '.join(args)}: {out.stderr.strip()[:300]}")
    return out


def inspect(container):
    return json.loads(docker("inspect", container).stdout)[0]


def host_cpus():
    return int(docker("info", "--format", "{{.NCPU}}").stdout.strip() or 1)


_SCALE = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3, "kB": 1e3, "MB": 1e6, "GB": 1e9}


def mem_usage_bytes(container):
    """Current memory usage from `docker stats`, e.g. '9.04MiB / 20MiB' -> 9479127."""
    out = docker("stats", "--no-stream", "--format", "{{.MemUsage}}", container).stdout
    m = re.match(r"\s*([\d.]+)\s*([A-Za-z]+)", out)
    return int(float(m.group(1)) * _SCALE.get(m.group(2), 1)) if m else 0


# --- state ------------------------------------------------------------------------

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"faults": {}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- inject -------------------------------------------------------------------------

def inject(fault_id):
    if fault_id not in FAULTS:
        raise KeyError(f"unknown fault {fault_id!r}; see `python -m faultctl list`")
    fault = FAULTS[fault_id]
    state = load_state()
    if fault_id in state["faults"]:
        return f"{fault_id} is already injected"
    if any(f["container"] == fault.container for f in state["faults"].values()):
        raise RuntimeError(f"{fault.container} already has a fault; reset first")

    info = inspect(fault.container)
    host = info["HostConfig"]
    entry = {"kind": fault.kind, "container": fault.container, "injected_at": now_iso(),
             "original": {"running": info["State"]["Running"], "nano_cpus": host.get("NanoCpus", 0),
                          "memory": host.get("Memory", 0), "memory_swap": host.get("MemorySwap", 0)}}
    # Record first, change second: a crash in between still leaves reset enough to undo it.
    state["faults"][fault_id] = entry
    save_state(state)

    if fault.kind == "stop":
        docker("stop", fault.container)
    elif fault.kind == "cpu_starve":
        docker("update", "--cpus", str(CPU_LIMIT), fault.container)
    elif fault.kind == "mem_starve":
        target = max(MEM_FLOOR, mem_usage_bytes(fault.container) // 2)
        entry["target_memory"] = target
        docker("update", "--memory", str(target), "--memory-swap", str(target), fault.container)
    elif fault.kind == "restart_loop":
        entry["pid"] = _spawn_loop(fault_id)
    save_state(state)
    return f"injected {fault_id}: {fault.description}"


def _spawn_loop(fault_id):
    cmd = [sys.executable, "-m", "faultctl", "_loop", fault_id]
    kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
              "cwd": str(HERE.parent)}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs).pid


CRASH_COMMANDS = [
    ["sh", "-c", "kill -TERM 1"],
    ["node", "-e", "process.kill(1, 'SIGTERM')"],
    ["python", "-c", "import os, signal; os.kill(1, signal.SIGTERM)"],
]


def crash_once(container):
    """Crash the container's main process so the restart policy restarts it."""
    for cmd in CRASH_COMMANDS:
        if docker("exec", container, *cmd, check=False).returncode == 0:
            return "sigterm"
    docker("kill", container, check=False)  # no runtime inside: simulate with kill + start
    time.sleep(3)
    docker("start", container, check=False)
    return "kill+start"


def run_loop(fault_id):
    """Background loop for restart_loop faults. Exits as soon as the fault leaves state.json."""
    while True:
        entry = load_state()["faults"].get(fault_id)
        if not entry:
            return
        crash_once(entry["container"])
        for _ in range(LOOP_INTERVAL):
            time.sleep(1)
            if fault_id not in load_state()["faults"]:
                return


# --- reset --------------------------------------------------------------------------

def _stop_loop(entry):
    pid = entry.get("pid")
    if not pid:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        else:
            os.kill(pid, 15)
    except OSError:
        pass  # already gone; the loop also exits by itself once the fault leaves state.json


def _undo(entry):
    c, orig = entry["container"], entry["original"]
    kind = entry["kind"]
    if kind == "restart_loop":
        _stop_loop(entry)
    if kind == "cpu_starve":
        # docker update cannot clear a CPU limit (--cpus 0 is ignored), so "no limit" is restored
        # as a limit of every host CPU, which is equivalent.
        cpus = orig["nano_cpus"] / 1e9 if orig["nano_cpus"] else host_cpus()
        docker("update", "--cpus", f"{cpus:g}", c)
    if kind == "mem_starve":
        args = ["update", "--memory", str(orig["memory"])]
        if orig["memory"]:
            args += ["--memory-swap", str(orig["memory_swap"])]
        docker(*args, c)
    if orig.get("running") and not inspect(c)["State"]["Running"]:
        docker("start", c)


def reset():
    """Undo every recorded fault, then make sure every catalogue container is running.
    Safe to run repeatedly and after a crash (driven by state.json)."""
    state = load_state()
    lines = []
    for fault_id, entry in list(state["faults"].items()):
        # Drop it from state first so a restart loop stops crashing the container.
        del state["faults"][fault_id]
        save_state(state)
        try:
            _undo(entry)
            lines.append(f"reset {fault_id}")
        except DockerError as e:
            state["faults"][fault_id] = entry  # keep it so the next reset retries
            save_state(state)
            lines.append(f"FAILED to reset {fault_id}: {e}")
    for c in sorted({f.container for f in FAULTS.values()}):
        try:
            if not inspect(c)["State"]["Running"]:
                docker("start", c)
                lines.append(f"started {c}")
        except DockerError as e:
            lines.append(f"could not check {c}: {e}")
    return lines or ["nothing to reset"]


# --- status / list --------------------------------------------------------------------

def status():
    state = load_state()["faults"]
    if not state:
        return ["no faults injected"]
    out = []
    for fault_id, e in state.items():
        try:
            i = inspect(e["container"])
            now = (f"{i['State']['Status']}, restarts={i['RestartCount']}, "
                   f"cpus={i['HostConfig'].get('NanoCpus', 0) / 1e9:g}, "
                   f"memory={i['HostConfig'].get('Memory', 0) // 1024 ** 2}MiB")
        except DockerError as err:
            now = f"inspect failed: {err}"
        out.append(f"{fault_id} since {e['injected_at']} -> {e['container']}: {now}")
    return out


def list_faults():
    return [f"{f.id:30} {f.description}" for f in FAULTS.values()]
