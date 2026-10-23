"""faultctl: inject and reset practice faults in the OpenTelemetry demo (our test harness).

Fully separate from the agent: the agent package never imports, reads or calls it, and a test
enforces that. Docker-only faults for now; every change is recorded in faultctl/state.json first
so `reset` can undo it even after a crash.
"""
from faultctl.core import FAULTS, inject, list_faults, reset, status

__all__ = ["FAULTS", "inject", "list_faults", "reset", "status"]
