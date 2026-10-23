"""faultctl with a fake docker: state saved before changes, idempotent reset, crash recovery."""
import json
from types import SimpleNamespace

import pytest

from faultctl import core


class FakeDocker:
    def __init__(self):
        self.calls = []
        self.containers = {c: {"running": True, "nano": 0, "mem": 160 * 2 ** 20, "swap": 320 * 2 ** 20}
                           for c in {f.container for f in core.FAULTS.values()}}

    def __call__(self, *args, check=True):
        self.calls.append(args)
        verb, c = args[0], args[-1]
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
        return {"State": {"Running": s["running"], "Status": "running" if s["running"] else "exited"},
                "RestartCount": 0,
                "HostConfig": {"NanoCpus": s["nano"], "Memory": s["mem"], "MemorySwap": s["swap"]}}


@pytest.fixture
def fake(monkeypatch, tmp_path):
    d = FakeDocker()
    monkeypatch.setattr(core, "STATE_FILE", tmp_path / "state.json")
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
