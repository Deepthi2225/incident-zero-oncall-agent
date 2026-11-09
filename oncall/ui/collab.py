"""Working on an incident together: who owns it, and the team's notes. Append-only runs/collab.jsonl."""
import json

from oncall import config
from oncall.record import append_line, now_iso


def _path():
    return config.RUNS_DIR / "collab.jsonl"


def _rows():
    try:
        lines = _path().read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def set_owner(incident, owner, by):
    append_line(_path(), {"ts": now_iso(), "incident": incident, "kind": "owner", "owner": owner, "by": by})


def add_note(incident, text, by):
    append_line(_path(), {"ts": now_iso(), "incident": incident, "kind": "note", "text": text, "by": by})


def all_owners():
    """{incident: its current owner}."""
    return {r.get("incident"): r.get("owner") for r in _rows() if r.get("kind") == "owner"}


def state(incident):
    """The incident's current owner and its timeline of notes and handovers, oldest first."""
    rows = [r for r in _rows() if r.get("incident") == incident]
    owners = [r for r in rows if r.get("kind") == "owner"]
    return {"owner": owners[-1].get("owner") if owners else None, "timeline": rows}
