"""The incident record: ONE JSON file per run (runs/<run_id>.json) holding everything the agent
saw, decided and did. The terminal summary and the markdown report are rendered from it.

Written incrementally and atomically after every phase (alert, each tool call, diagnosis, each
plan step event, outcome), so a crash or Ctrl+C leaves a valid partial file with status
"interrupted". Every write is scrubbed: secrets redacted, feature-flag data removed.

Format: schemas/incident.schema.json (schema_version 2). Version-1 records (Day 1) are converted
on read by upgrade().
"""
import json
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from oncall import config
from oncall.sources.sanitize import strip_flags

SCHEMA_VERSION = 2
FINAL_STATUSES = ["recovered", "partial", "not_recovered", "inconclusive", "escalated"]
# USD per million tokens (Anthropic first-party rates). Thinking tokens bill as output.
PRICES_PER_MTOK = {"claude-opus-5-5": (4.00, 20.00), "claude-sonnet-5-5": (2.00, 10.00),
                   "claude-haiku-5-5": (0.10, 0.50)}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso(ts):
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")
    return ts


# --- environment facts ----------------------------------------------------------------------------

def git_info():
    """Commit of this repo and whether the working tree has uncommitted changes (read-only)."""
    def git(*args):
        return subprocess.run(["git", *args], cwd=config.ROOT, capture_output=True, text=True,
                              timeout=10).stdout.strip()
    try:
        return {"commit": git("rev-parse", "HEAD") or None,
                "dirty": bool(git("status", "--porcelain", "--untracked-files=no"))}
    except (OSError, subprocess.TimeoutExpired):
        return {"commit": None, "dirty": None}


def estimate_cost(model, tokens_in, tokens_out):
    price = PRICES_PER_MTOK.get(model)
    if not price:
        return None
    return round(tokens_in / 1e6 * price[0] + tokens_out / 1e6 * price[1], 4)


# --- scrubbing --------------------------------------------------------------------------------------

_SECRET_KEY = re.compile(r"^(x-)?api[_-]?key$|^authorization$|^password$|^secret$|(^|_)token$|^cookie$", re.I)
_SECRET_VALUE = [re.compile(r"sk-ant-[A-Za-z0-9_\-]{6,}"), re.compile(r"Bearer\s+[A-Za-z0-9._~+/=\-]{6,}", re.I)]
# feature_flag as its own word (not the action name toggle_feature_flag)
FLAG_TEXT = re.compile(r"(?<![A-Za-z0-9_])feature_flag", re.I)
REDACTED, FLAG_REMOVED = "<redacted>", "<removed: feature-flag data>"


def _known_secrets():
    values = [os.environ.get("ANTHROPIC_API_KEY"), os.environ.get("ANTHROPIC_AUTH_TOKEN"),
              os.environ.get("GEMINI_API_KEY"), config.ACTION_API_TOKEN]
    return [v for v in values if v and len(v) >= 6]


def scrub(obj, _secrets=None):
    """Redact secrets and remove feature-flag data anywhere in obj (HARD RULE 2)."""
    secrets = _known_secrets() if _secrets is None else _secrets
    obj = strip_flags(obj) if _secrets is None else obj
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if FLAG_TEXT.match(str(k)):
                continue
            out[k] = REDACTED if _SECRET_KEY.search(str(k)) and isinstance(v, str) and v else scrub(v, secrets)
        return out
    if isinstance(obj, list):
        return [scrub(v, secrets) for v in obj]
    if isinstance(obj, str):
        for s in secrets:
            obj = obj.replace(s, REDACTED)
        for pattern in _SECRET_VALUE:
            obj = pattern.sub(REDACTED, obj)
        return FLAG_REMOVED if FLAG_TEXT.search(obj) else obj
    return obj


# --- atomic files -----------------------------------------------------------------------------------

def atomic_write(path, text):
    """Write via a temp file in the same folder, then os.replace (atomic on Windows and POSIX).
    Retries briefly when Windows reports the target busy (e.g. open in an editor)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(10):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def append_line(path, entry):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(scrub(entry), default=str) + "\n")


# --- views of the in-memory objects --------------------------------------------------------------

CITING = ("root_causes", "anomalies", "affected_services", "ruled_out")


def cited_ids(diagnosis):
    ids = set()
    if not diagnosis:
        return ids
    for key in CITING:
        for item in diagnosis.get(key) or []:
            ids.update(item.get("evidence") or [])
    ids.update((diagnosis.get("suspected_origin") or {}).get("evidence") or [])
    return ids


def evidence_view(items, cited):
    return [{"id": e["id"], "tool": e.get("tool"), "kind": e.get("kind"),
             "observation": e.get("observation", e.get("summary", "")), "ts": _iso(e.get("ts")),
             "cited": e["id"] in cited, "data": e.get("data", {})} for e in items]


def diagnosis_view(diag):
    """The validated diagnosis without the plan (which has its own section)."""
    if diag is None:
        return None
    keep = ("status", "headline", "confidence", "root_causes", "suspected_origin", "affected_services",
            "anomalies", "ruled_out", "open_questions", "prevention", "proposed_action", "validation")
    d = {k: diag.get(k) for k in keep}
    d["ruled_out"] = d["ruled_out"] or []
    d["open_questions"] = d["open_questions"] or []
    d["validation"] = {"dropped_evidence_ids": [], "notes": [], "overrides": [], **(d["validation"] or {})}
    return d


def plan_view(plan):
    if plan is None:
        return None
    return {k: v for k, v in plan.items() if k != "escalations"}


def escalations_view(plan):
    if not plan:
        return []
    out = [{"target": e.get("target"), "fault_class": e.get("fault_class"), "reason": e.get("reason"),
            "next_steps": e.get("next_steps", []), "source": "no allowlisted fix"}
           for e in plan.get("escalations", [])]
    out += [{"target": n["target"], "fault_class": n.get("fault_class"), "reason": n["why"],
             "next_steps": [f"Decide on {n['action']} on {n['target']} by hand."], "source": "plan stopped"}
            for n in plan.get("not_run", [])]
    return out


# --- the final outcome --------------------------------------------------------------------------------

def compute_outcome(record):
    """(status, summary) from the record's own fields."""
    inv = (record.get("investigation") or {}).get("outcome")
    diag = record.get("diagnosis") or {}
    plan = record.get("remediation_plan") or {}
    rcs = diag.get("root_causes") or []
    head = (f"{rcs[0]['component']} [{rcs[0]['fault_class']}] diagnosed at {diag.get('confidence')}"
            if rcs else f"no root cause with valid evidence (diagnosis {diag.get('status', 'missing')})")
    if inv != "submitted":
        return "escalated", f"investigation ended without a diagnosis ({inv}); escalated to a human"
    manual = record.get("manual_verifications") or []
    if manual:
        return manual[-1]["outcome"], f"{head}; fixed by hand, verify-only result: {manual[-1]['outcome']}"
    steps, outcome = plan.get("steps", []), plan.get("outcome")
    resolved = plan.get("resolved") or []
    if not steps and resolved and not record.get("escalations"):
        return "escalated", (f"{head}; all {len(resolved)} root cause(s) resolved before action, nothing "
                             f"executed; a human should confirm")
    acted = "; ".join(f"{s['action']} {s['target']}: {s.get('outcome', 'not run')}" for s in steps)
    others = len(record.get("escalations") or [])
    if outcome == "recovered":
        if others:
            return "partial", f"{head}; {acted}; {others} item(s) escalated to a human"
        return "recovered", f"{head}; {acted}"
    if outcome == "dry_run":
        return "escalated", f"{head}; dry run: {len(steps)} step(s) proposed, nothing executed"
    if outcome == "stopped":
        verdicts = [s.get("outcome") for s in steps if s.get("outcome") in
                    ("partial", "not_recovered", "inconclusive")]
        status = verdicts[-1] if verdicts else "escalated"
        return status, f"{head}; {acted}; stopped: {plan.get('stopped_reason')}"
    if outcome == "partial":
        return "partial", f"{head}; {acted}"
    return "escalated", f"{head}; no allowlisted action, escalated to a human"


def action_log_lines(run_id):
    """1-based line numbers of this run's entries in the action log."""
    if not config.ACTION_LOG.exists():
        return []
    lines = []
    with open(config.ACTION_LOG, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            try:
                if json.loads(line).get("run_id") == run_id:
                    lines.append(n)
            except ValueError:
                continue
    return lines


def index_entry(record):
    meta, diag = record["metadata"], record.get("diagnosis") or {}
    primary = max(diag.get("root_causes") or [{}], key=lambda rc: rc.get("confidence", 0))
    return {"run_id": meta["run_id"], "incident_id": meta.get("incident_id"), "time": meta.get("ended_at"),
            "mode": meta["mode"], "fault_class": primary.get("fault_class"),
            "component": primary.get("component"), "confidence": diag.get("confidence"),
            "status": (record.get("outcome") or {}).get("status"),
            "cost_usd": record["accounting"].get("estimated_cost_usd")}


# --- the live record ---------------------------------------------------------------------------------

def record_path(run_id, source):
    suffix = "" if source == "live" else f"-{source}"
    return config.RUNS_DIR / f"{run_id}{suffix}.json"


def unique_run_id(base, source):
    """base (a timestamp) or base-2, base-3... so two runs in the same second never share a file."""
    run_id, n = base, 1
    while record_path(run_id, source).exists():
        n += 1
        run_id = f"{base}-{n}"
    return run_id


class RunRecord:
    """Builds and saves the record of one agent run. self.saved is exactly what is on disk."""

    def __init__(self, run_id, alert, info, model, max_steps, provider="anthropic"):
        self.t0 = time.time()
        self.path = record_path(run_id, info["source"])
        self.report_path = self.path.with_suffix(".report.md")
        self.diag, self.registry = None, None
        self.timers = {"investigation_s": None, "remediation_s": None}
        alert = alert if isinstance(alert, dict) else {"alert": alert}
        self.data = {
            "schema_version": SCHEMA_VERSION, "status": "running",
            "metadata": {"run_id": run_id,
                         "incident_id": alert.get("incident_id") or f"run-{run_id}",
                         "started_at": now_iso(), "ended_at": None, "updated_at": None,
                         "mode": info["source"], "source_detail": info["source_detail"],
                         "provider": provider, "model": model, "git": git_info()},
            "alert": alert,
            "investigation": {"steps": [], "outcome": None, "error": None, "model_turns": 0},
            "evidence": [], "diagnosis": None, "remediation_plan": None, "escalations": [],
            "manual_verifications": [], "outcome": None, "review": None,
            "accounting": {"tokens_in": 0, "tokens_out": 0, "estimated_cost_usd": 0.0,
                           "reviewer_tokens": {"in": 0, "out": 0},
                           "price_basis": "USD per 1M tokens, Anthropic first-party rates",
                           "phases": {"investigation_s": None, "remediation_s": None, "verify_s": None,
                                      "total_s": None},
                           "steps_used": 0, "step_budget": max_steps},
            "report_paths": {"report": str(self.report_path), "actions_log": str(config.ACTION_LOG),
                             "action_log_lines": []},
        }
        self.saved = None

    # phases -----------------------------------------------------------------------------------
    def model_turn(self, tokens_in, tokens_out):
        inv, acc = self.data["investigation"], self.data["accounting"]
        inv["model_turns"] += 1
        acc["steps_used"] = inv["model_turns"]
        acc["tokens_in"] += tokens_in
        acc["tokens_out"] += tokens_out

    def review_tokens(self, tokens_in, tokens_out):
        """The reviewer's call: counted in the run's tokens and cost, but not as an investigation turn."""
        acc = self.data["accounting"]
        acc["tokens_in"] += tokens_in
        acc["tokens_out"] += tokens_out
        acc["reviewer_tokens"] = {"in": acc["reviewer_tokens"]["in"] + tokens_in,
                                  "out": acc["reviewer_tokens"]["out"] + tokens_out}

    def tool_call(self, **step):
        self.data["investigation"]["steps"].append(step)
        self.save()

    def investigation_done(self, outcome, error=None):
        self.data["investigation"].update(outcome=outcome, error=error)
        self.timers["investigation_s"] = round(time.time() - self.t0, 1)
        self.data["investigation"]["submitted_at_s"] = self.timers["investigation_s"]

    def set_diagnosis(self, diag):
        self.diag = diag
        self.save()

    def plan_updated(self):
        self.save()

    def finish(self):
        self.timers["remediation_s"] = round(time.time() - self.t0 - (self.timers["investigation_s"] or 0), 1)
        self.save(status="complete")
        status, summary = compute_outcome(self.saved)
        self.data["outcome"] = {"status": status, "summary": summary,
                                "prevention": (self.saved.get("diagnosis") or {}).get("prevention") or [],
                                "investigation_outcome": self.saved["investigation"]["outcome"],
                                "plan_outcome": (self.saved.get("remediation_plan") or {}).get("outcome")}
        self.data["metadata"]["ended_at"] = now_iso()
        self.save(status="complete")
        write_report(self.saved, self.report_path)
        append_line(config.RUNS_DIR / "index.jsonl", index_entry(self.saved))
        return self.saved

    def interrupt(self, reason="interrupted"):
        self.data["investigation"].setdefault("error", None)
        self.data["interrupted_reason"] = reason
        self.save(status="interrupted")
        try:
            write_report(self.saved, self.report_path)
        except Exception:  # the JSON is what matters; a partial report is a bonus
            pass

    # writing -----------------------------------------------------------------------------------
    def save(self, status=None):
        from oncall.evidence import registry
        d = self.data
        if status:
            d["status"] = status
        d["metadata"]["updated_at"] = now_iso()
        d["diagnosis"] = diagnosis_view(self.diag)
        d["review"] = (self.diag or {}).get("review")
        plan = (self.diag or {}).get("remediation_plan")
        d["remediation_plan"] = plan_view(plan)
        d["escalations"] = escalations_view(plan)
        d["evidence"] = evidence_view(registry.dump(), cited_ids(d["diagnosis"]))
        acc = d["accounting"]
        acc["estimated_cost_usd"] = estimate_cost(d["metadata"]["model"], acc["tokens_in"], acc["tokens_out"])
        verify_s = sum(((s.get("verify") or {}).get("seconds") or 0) for s in (plan or {}).get("steps", []))
        acc["phases"] = {"investigation_s": self.timers["investigation_s"],
                         "remediation_s": self.timers["remediation_s"],
                         "verify_s": round(verify_s, 1), "total_s": round(time.time() - self.t0, 1)}
        d["report_paths"]["action_log_lines"] = action_log_lines(d["metadata"]["run_id"])
        clean = scrub(json.loads(json.dumps(d, default=str)))
        atomic_write(self.path, json.dumps(clean, indent=2))
        self.saved = clean
        return clean


def write_report(record, path):
    from oncall.report import build_report
    atomic_write(path, build_report(record))


# --- reading old and existing records ---------------------------------------------------------------

def upgrade(record):
    """Version-1 records (Day 1: run_id/source/summary/steps at the top) -> version 2, best effort."""
    if record.get("schema_version") == SCHEMA_VERSION:
        return record
    summary = dict(record.get("summary") or {})
    plan = summary.pop("remediation_plan", None)
    if plan is None:
        act = summary.get("proposed_action") or {}
        if act.get("action") in (None, "escalate_to_human", "none"):
            plan = {"steps": [], "escalations": [act] if act else [], "outcome": "escalated"}
        else:
            step = {**act, "step": 1, "approval": summary.get("approval")}
            plan = {"steps": [step], "escalations": [], "outcome": "dry_run"}
    for i, step in enumerate(plan.get("steps", []), 1):  # fields that version 1 did not have
        step.setdefault("step", i)
        step.setdefault("params", {"flag": step["flag"]} if step.get("flag") else {})
        step.setdefault("reason", "")
        if step.get("approval") is None:
            step.pop("approval", None)
    diagnosis = diagnosis_view({**summary, "status": summary.get("status")})
    steps = [{"step": s.get("step"), "tool": s.get("tool"), "arguments": s.get("input"),
              "result": s.get("result"), "evidence_ids": [], "seconds": s.get("seconds"),
              "at_s": s.get("at_s"), "error": (s.get("result") or {}).get("error") if s.get("is_error") else None}
             for s in record.get("steps", [])]
    tokens = record.get("usage") or {}
    new = {
        "schema_version": SCHEMA_VERSION, "status": "complete", "upgraded_from": 1,
        "metadata": {"run_id": record.get("run_id"),
                     "incident_id": (record.get("alert") or {}).get("incident_id") or f"run-{record.get('run_id')}",
                     "started_at": record.get("started_at"), "ended_at": None, "updated_at": None,
                     "mode": record.get("source"), "source_detail": record.get("source_detail"),
                     "provider": "anthropic", "model": record.get("model"), "git": {"commit": None, "dirty": None}},
        "alert": record.get("alert") or {},
        "investigation": {"steps": steps, "outcome": record.get("outcome"), "error": record.get("error"),
                          "model_turns": None},
        "evidence": evidence_view(record.get("evidence", []), cited_ids(diagnosis)),
        "diagnosis": diagnosis, "remediation_plan": plan_view(plan), "escalations": escalations_view(plan),
        "manual_verifications": record.get("manual_verifications", []), "outcome": None,
        "accounting": {"tokens_in": tokens.get("input", 0), "tokens_out": tokens.get("output", 0),
                       "estimated_cost_usd": estimate_cost(record.get("model"), tokens.get("input", 0),
                                                           tokens.get("output", 0)),
                       "price_basis": "USD per 1M tokens, Anthropic first-party rates",
                       "phases": {"investigation_s": record.get("diagnosed_at_s"), "remediation_s": None,
                                  "verify_s": None, "total_s": record.get("seconds")},
                       "steps_used": None, "step_budget": None},
        "report_paths": {"report": None, "actions_log": None, "action_log_lines": []},
    }
    status, line = compute_outcome(new)
    new["outcome"] = {"status": status, "summary": line, "prevention": (diagnosis or {}).get("prevention") or [],
                      "investigation_outcome": record.get("outcome"), "plan_outcome": (plan or {}).get("outcome")}
    return new


def load(path):
    return upgrade(json.loads(Path(path).read_text(encoding="utf-8")))


def save_existing(record, path):
    """Rewrite an existing record (e.g. after verify-only) atomically, with outcome and report."""
    from oncall.evidence import registry
    record["evidence"] = evidence_view(registry.dump(), cited_ids(record.get("diagnosis")))
    status, line = compute_outcome(record)
    record["outcome"] = {**(record.get("outcome") or {}), "status": status, "summary": line}
    record["metadata"]["updated_at"] = now_iso()
    record["report_paths"]["action_log_lines"] = action_log_lines(record["metadata"]["run_id"])
    clean = scrub(json.loads(json.dumps(record, default=str)))
    atomic_write(path, json.dumps(clean, indent=2))
    write_report(clean, Path(path).with_suffix(".report.md"))
    return clean
