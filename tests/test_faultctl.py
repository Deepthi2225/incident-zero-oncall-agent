"""faultctl with a fake docker: state saved before changes, idempotent reset, crash recovery."""
import json
from types import SimpleNamespace

import pytest

from faultctl import core


class FakeDocker:
    def __init__(self):
        self.calls = []
        self.containers = {c: {"running": True, "nano": 0, "mem": 160 * 2 ** 20, "swap": 320 * 2 ** 20}
                           for c in {f.container for f in core.FAULTS.values()} | {"frontend"}}
        self.started_at = {c: "t0" for c in self.containers}
        self.exec_effect = True  # does an in-container SIGTERM actually restart the container?

    def __call__(self, *args, check=True):
        self.calls.append(args)
        verb, c = args[0], args[-1]
        if verb == "ps":
            return SimpleNamespace(stdout="\n".join(sorted(self.containers)), returncode=0)
        if verb == "exec":
            if self.exec_effect:
                self.started_at[args[1]] = "t1"
            return SimpleNamespace(stdout="", returncode=0)
        if verb == "stop":
            self.containers[c]["running"] = False
        elif verb == "start":
            self.containers[c]["running"] = True
        elif verb == "update":
            opts = dict(zip(args[1:-1:2], args[2:-1:2]))
            if "--cpus" in opts:
                self.containers[c]["nano"] = int(float(opts["--cpus"]) * 1e9)
            if "--memory" in opts:
                self.containers[c]["mem"] = int(opts["--memory"])
        elif verb == "stats":
            return SimpleNamespace(stdout="40MiB / 160MiB", returncode=0)
        elif verb == "info":
            return SimpleNamespace(stdout="20\n", returncode=0)
        return SimpleNamespace(stdout="", returncode=0)

    def inspect(self, c):
        s = self.containers[c]
        return {"State": {"Running": s["running"], "Status": "running" if s["running"] else "exited",
                          "StartedAt": self.started_at[c]},
                "Config": {"Labels": {"com.docker.compose.project": "demo"}},
                "RestartCount": 0,
                "HostConfig": {"NanoCpus": s["nano"], "Memory": s["mem"], "MemorySwap": s["swap"]}}


@pytest.fixture
def fake(monkeypatch, tmp_path):
    d = FakeDocker()
    monkeypatch.setattr(core, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(core, "ORIGINALS_FILE", tmp_path / "originals.json")
    monkeypatch.setattr(core, "loop_pids", lambda: [])
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    monkeypatch.setattr(core, "docker", d)
    monkeypatch.setattr(core, "inspect", d.inspect)
    return d


def test_catalogue_has_all_requested_faults():
    ids = set(core.FAULTS)
    for svc in ["payment", "cart", "checkout", "product-catalog", "recommendation", "ad"]:
        assert {f"stop-{svc}", f"restart-loop-{svc}", f"cpu-starve-{svc}", f"mem-starve-{svc}"} <= ids
    assert {"cache-down", "db-down"} <= ids
    assert core.FAULTS["cache-down"].container == "valkey-cart"
    assert core.FAULTS["db-down"].container == "astronomy-db"


def test_cpu_starve_saves_original_and_reset_restores(fake):
    core.inject("cpu-starve-ad")
    state = json.loads(core.STATE_FILE.read_text())
    assert state["faults"]["cpu-starve-ad"]["original"]["nano_cpus"] == 0
    assert fake.containers["ad"]["nano"] == int(0.1 * 1e9)
    assert core.reset() == ["reset cpu-starve-ad"]
    assert fake.containers["ad"]["nano"] == 20 * 10 ** 9  # "no limit" restored as all 20 host CPUs
    assert core.reset() == ["nothing to reset"]  # safe to repeat


def test_mem_starve_restores_memory_and_swap(fake):
    core.inject("mem-starve-cart")
    assert fake.containers["cart"]["mem"] == 20 * 2 ** 20  # half of 40 MiB usage
    core.reset()
    assert fake.containers["cart"]["mem"] == 160 * 2 ** 20
    assert ("update", "--memory", str(160 * 2 ** 20), "--memory-swap", str(320 * 2 ** 20), "cart") in fake.calls


def test_reset_after_crash_uses_state_and_starts_stopped_containers(fake):
    core.inject("cache-down")
    assert not fake.containers["valkey-cart"]["running"]
    # Simulate a crash: the process that injected is gone, only state.json remains.
    lines = core.reset()
    assert "reset cache-down" in lines and fake.containers["valkey-cart"]["running"]
    # A container stopped outside faultctl is started too.
    fake.containers["payment"]["running"] = False
    assert core.reset() == ["started payment"]


def test_one_fault_per_container_and_unknown_ids(fake):
    core.inject("stop-cart")
    with pytest.raises(RuntimeError):
        core.inject("cpu-starve-cart")
    assert "already injected" in core.inject("stop-cart")
    with pytest.raises(KeyError):
        core.inject("stop-everything")
    core.reset()


def test_reset_starts_any_stopped_demo_container(fake):
    fake.containers["frontend"]["running"] = False  # not a faultctl target, stopped for any reason
    assert "started frontend" in core.reset()
    assert core.reset() == ["nothing to reset"]


def test_limits_restored_even_if_state_is_lost(fake):
    core.inject("cpu-starve-cart")
    core.STATE_FILE.unlink()  # crash: state.json gone, originals.json survives
    lines = core.reset()
    assert "restored cart cpus to 20" in lines and fake.containers["cart"]["nano"] == 20 * 10 ** 9
    core.inject("mem-starve-ad")
    core.STATE_FILE.unlink()
    assert "restored ad memory to 160MiB" in core.reset()
    assert fake.containers["ad"]["mem"] == 160 * 2 ** 20


def test_reset_kills_orphaned_restart_loops(fake, monkeypatch):
    killed = []
    monkeypatch.setattr(core, "loop_pids", lambda: [4242])
    monkeypatch.setattr(core, "_stop_loop", lambda e: killed.append(e["pid"]))
    assert "killed leftover restart loop (pid 4242)" in core.reset() and killed == [4242]


def test_crash_falls_back_when_sigterm_has_no_effect(fake):
    fake.exec_effect = False  # e.g. node as PID 1 ignores SIGTERM without a handler
    assert core.crash_once("payment") == "kill+start"
    assert ("kill", "payment") in fake.calls and ("start", "payment") in fake.calls
    fake.exec_effect = True
    assert core.crash_once("cart") == "sigterm"
