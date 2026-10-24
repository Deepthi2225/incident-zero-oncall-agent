"""Evidence registry.

Every fact a tool observes is stored here and gets an ID (E1, E2, ...). The diagnosis may cite
only IDs that exist in the registry; validate() drops anything else, so the agent cannot invent
evidence.
"""
import itertools
import json
import threading
import time


class Registry:
    def __init__(self):
        self._lock = threading.Lock()
        self._items = {}
        self._counter = itertools.count(1)

    def add(self, tool, kind, summary, data=None):
        """Register one observation and return its ID.

        tool:    tool that produced it, e.g. "find_error_traces"
        kind:    "metric" | "trace" | "log" | "container" | "deploy" | "other"
        summary: one human-readable line, quoted in reports
        data:    the exact supporting values (trace_id, log line, metric numbers)
        """
        with self._lock:
            eid = f"E{next(self._counter)}"
            self._items[eid] = {
                "id": eid, "tool": tool, "kind": kind, "summary": summary,
                "data": data if data is not None else {}, "ts": time.time(),
            }
        return eid

    def get(self, eid):
        return self._items.get(eid)

    def all(self):
        return list(self._items.values())

    def __contains__(self, eid):
        return eid in self._items

    def __len__(self):
        return len(self._items)

    def validate(self, ids):
        """Split cited IDs into (valid, dropped)."""
        valid, dropped = [], []
        for eid in ids or []:
            (valid if eid in self._items else dropped).append(eid)
        return valid, dropped

    def reset(self):
        with self._lock:
            self._items.clear()
            self._counter = itertools.count(1)

    def load(self, items):
        """Restore a saved run's evidence; new IDs continue after the highest one (E12 -> E13)."""
        with self._lock:
            # records store the text as "observation"; the registry calls it "summary"
            self._items = {e["id"]: {**e, "summary": e.get("summary", e.get("observation", ""))}
                           for e in items}
            top = max((int(e["id"][1:]) for e in items if e["id"][1:].isdigit()), default=0)
            self._counter = itertools.count(top + 1)

    def dump(self):
        return json.loads(json.dumps(self.all(), default=str))


# One registry per process; the agent resets it at the start of every run.
registry = Registry()
