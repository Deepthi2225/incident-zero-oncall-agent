"""The postmortem of one incident, derived ONLY from its record (runs/<run_id>.json).

    build_postmortem(record) -> {"draft", "times", "durations", "impact", "five_whys",
                                 "went_well", "could_be_better", "action_items"}

Nothing is invented: a time, number or "why" that the record does not contain is None (shown as
"unknown"). Times are ISO 8601 UTC strings; durations are seconds. Old (Day 1) records work too.
Stored in the record under "postmortem" and rendered by report.build_report and the dashboard.
"""
import re
from datetime import datetime, timezone

from oncall import config

UNKNOWN_WHY = "unknown, needs human investigation"
SEVERITY_PRIORITY = {"critical": "high", "high": "high", "medium": "medium", "low": "low"}
_DETECT = re.compile(r"\b(alert|alarm|monitor|detect|dashboard|probe|slo|page|notify|watch)", re.I)
_MITIGATE = re.compile(r"\b(restart|rollback|roll back|runbook|failover|fallback|retry|retries|circuit|"
                       r"throttl|rate.?limit|scale|autoscal|timeout|degrad)", re.I)
_FRACTION = re.compile(r"(\.\d{6})\d+")


def parse_time(value):
    """An aware UTC datetime from the ISO-ish strings the tools record, else None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = _FRACTION.sub(r"\1", value.strip().replace("Z", "+00:00"))
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.year < 2000:  # docker's "0001-01-01T00:00:00Z" means "never"
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def _seconds(a, b):
    """b - a in seconds; None when either is missing or the order is impossible (clock mix-up)."""
    if not (a and b) or b < a:
        return None
    return round((b - a).total_seconds(), 1)


# --- times ------------------------------------------------------------------------------------------

def _components(record):
    d = record.get("diagnosis") or {}
    names = {rc.get("component") for rc in d.get("root_causes") or [] if isinstance(rc, dict)}
    names |= {a.get("service") for a in d.get("affected_services") or [] if isinstance(a, dict)}
    return {n for n in names if n}


def _evidence_times(e):
    """(time, what) pairs that show a fault in one evidence item."""
    data = e.get("data") or {}
    out = []
    if data.get("stopped_at") and str(data.get("status", "")).lower() not in ("running", ""):
        out.append((parse_time(data["stopped_at"]), f"{data.get('name', '?')} container stopped"))
    if data.get("level") in ("error", "fatal", "critical") and data.get("first_seen"):
        out.append((parse_time(data["first_seen"]), f"first {data['level']} log line in {data.get('service', '?')}"))
    return [(t, what) for t, what in out if t]


def fault_start(record):
    """(time, evidence id, what) of the earliest evidence of the fault, or (None, None, None).
    Cited evidence about the diagnosed components first, then any evidence about them."""
    evidence = record.get("evidence") or []
    comps = _components(record)

    def about(e):
        data = e.get("data") or {}
        return not comps or data.get("name") in comps or data.get("service") in comps

    for pool in ([e for e in evidence if e.get("cited") and about(e)], [e for e in evidence if about(e)]):
        found = [(t, e.get("id"), what) for e in pool for t, what in _evidence_times(e)]
        if found:
            return min(found, key=lambda f: f[0])
    return None, None, None


def key_times(record):
    meta, inv = record.get("metadata") or {}, record.get("investigation") or {}
    alert = record.get("alert") or {}
    steps = (record.get("remediation_plan") or {}).get("steps") or []
    started = parse_time(meta.get("started_at"))
    fault, fault_ev, fault_what = fault_start(record)
    detected = parse_time(alert.get("detected_at") or alert.get("fired_at"))
    diagnosed = None
    if started and inv.get("submitted_at_s") is not None and inv.get("outcome") == "submitted":
        diagnosed = datetime.fromtimestamp(started.timestamp() + float(inv["submitted_at_s"]), timezone.utc)
    approvals = [parse_time((s.get("approval") or {}).get("decision_at")) for s in steps
                 if (s.get("approval") or {}).get("status") == "approved"]
    executed = [parse_time((s.get("execution") or {}).get("at")) for s in steps
                if (s.get("execution") or {}).get("status") == "executed"]
    verified = None
    plan_outcome = (record.get("remediation_plan") or {}).get("outcome")
    recovered_steps = [s for s in steps if (s.get("verify") or {}).get("outcome") == "recovered"]
    if plan_outcome == "recovered" and recovered_steps:
        last = recovered_steps[-1]
        checks = [parse_time(c.get("at")) for c in (last.get("verify") or {}).get("checks") or []]
        checks = [c for c in checks if c]
        ex_at = parse_time((last.get("execution") or {}).get("at"))
        secs = (last.get("verify") or {}).get("seconds")
        verified = checks[-1] if checks else (
            datetime.fromtimestamp(ex_at.timestamp() + float(secs), timezone.utc) if ex_at and secs is not None else None)
    manual = [m for m in record.get("manual_verifications") or [] if m.get("outcome") == "recovered"]
    if manual:
        verified = parse_time(manual[-1].get("at")) or verified
    return {"fault_started": _iso(fault), "fault_started_evidence": fault_ev, "fault_started_what": fault_what,
            "detected": _iso(detected), "investigation_started": _iso(started), "diagnosed": _iso(diagnosed),
            "approved": _iso(min([a for a in approvals if a], default=None)),
            "executed": _iso(min([x for x in executed if x], default=None)),
            "recovery_verified": _iso(verified), "ended": _iso(parse_time(meta.get("ended_at")))}


def durations(times):
    t = {k: parse_time(v) for k, v in times.items() if k.endswith(("started", "detected", "diagnosed",
                                                                   "verified", "ended"))}
    detected = t.get("detected")
    begin = t.get("fault_started") or detected or t.get("investigation_started")
    end = t.get("recovery_verified") or t.get("ended")
    return {"time_to_detect_s": _seconds(t.get("fault_started"), detected),
            "time_to_diagnose_s": _seconds(detected or t.get("investigation_started"), t.get("diagnosed")),
            "time_to_recover_s": _seconds(detected, t.get("recovery_verified")),
            "total_s": _seconds(begin, end)}


# --- impact -----------------------------------------------------------------------------------------

def _pct(x):
    return f"{x * 100:.1f}%" if isinstance(x, (int, float)) else "unknown"


def impact(record):
    d = record.get("diagnosis") or {}
    services = [{"service": a.get("service"), "impact": a.get("impact"), "evidence": list(a.get("evidence") or [])}
                for a in d.get("affected_services") or [] if isinstance(a, dict)]
    signals = [{"text": f"{a.get('service')} {a.get('signal')}: {a.get('observed')} (baseline {a.get('baseline')})",
                "evidence": list(a.get("evidence") or [])}
               for a in d.get("anomalies") or [] if isinstance(a, dict)]
    failed = []
    for e in record.get("evidence") or []:
        data = e.get("data") or {}
        row = data.get("row") if isinstance(data.get("row"), dict) else {}
        if e.get("tool") == "get_service_health" and row.get("service") and row.get("flags"):
            base, flags = row.get("baseline") or {}, row["flags"]
            parts = []
            if "errors" in flags:
                parts.append(f"error rate {_pct(row.get('error_rate'))} (baseline {_pct(base.get('error_rate'))})")
            if "client_call_errors" in flags:
                parts.append(f"client-call error rate {_pct(row.get('client_error_rate'))} "
                             f"(baseline {_pct(base.get('client_error_rate'))})")
            if "traffic_drop" in flags or "silent" in flags:
                ratio = row.get("rps_ratio")
                parts.append(f"traffic {row.get('rps')} rps against a baseline of {base.get('rps')} rps"
                             + (f" ({ratio * 100:.0f}%)" if isinstance(ratio, (int, float)) else ""))
            if parts:
                signals.append({"text": f"{row['service']}: " + "; ".join(parts), "evidence": [e["id"]]})
        if e.get("tool") == "find_error_traces" and data.get("origin_service") and data.get("count"):
            failed.append({"text": f"{data['count']} failing trace(s) originating in {data['origin_service']} "
                                   f"'{data.get('operation')}' (sampled traces, not all requests)",
                           "evidence": [e["id"]]})
        if e.get("tool") == "search_logs" and data.get("level") in ("error", "fatal", "critical") and data.get("count"):
            failed.append({"text": f"{data['count']} {data['level']} log line(s) in {data.get('service')}",
                           "evidence": [e["id"]]})
    return {"services": services, "signals": signals, "failed_requests": failed,
            "failed_requests_total": None,  # the record holds rates and samples, never a full count
            "note": "Failed-request numbers are counts from sampled traces and log lines in the record; "
                    "the total number of failed requests is unknown."}


# --- 5 whys -----------------------------------------------------------------------------------------

def five_whys(record):
    """Up to five whys, each citing evidence IDs; stops at UNKNOWN_WHY when the evidence runs out.
    Uses the model's own five_whys when the diagnosis has them (evidence IDs checked)."""
    d = record.get("diagnosis") or {}
    known = {e.get("id") for e in record.get("evidence") or []}
    given = d.get("five_whys")
    if isinstance(given, list) and given:
        rows = []
        for w in given[:5]:
            if isinstance(w, dict) and w.get("because"):
                ev = [i for i in w.get("evidence") or [] if i in known]
                rows.append({"why": str(w.get("why", "")), "because": str(w["because"]) if ev else UNKNOWN_WHY,
                             "evidence": ev, "source": "model"})
        if rows:
            return _pad(rows)

    rows = []
    alert = record.get("alert") or {}
    affected = [a for a in d.get("affected_services") or [] if isinstance(a, dict)]
    alerting = ", ".join(alert.get("alerting_services") or ([alert["service"]] if alert.get("service") else [])) \
        or "a service"
    if affected and affected[0].get("evidence"):
        a = affected[0]
        rows.append({"why": f"Why did the alert fire on {alerting}?",
                     "because": f"{a.get('service')}: {a.get('impact')}", "evidence": list(a["evidence"]),
                     "source": "derived"})
    rcs = sorted([rc for rc in d.get("root_causes") or [] if isinstance(rc, dict) and rc.get("evidence")],
                 key=lambda rc: -(rc.get("confidence") or 0))
    if rcs and d.get("status") == "diagnosed":
        rc = rcs[0]
        subject = rows[-1]["because"].split(":")[0] if rows else alerting
        rows.append({"why": f"Why did {subject} fail?",
                     "because": f"{rc['component']} [{rc.get('fault_class')}]: {rc.get('cause')}",
                     "evidence": list(rc["evidence"]), "source": "derived"})
        detail = _mechanism(record, rc)
        if detail:
            rows.append({"why": f"Why did {rc['component']} fail that way?", "because": detail[1],
                         "evidence": [detail[0]], "source": "derived"})
    return _pad(rows)


def _mechanism(record, rc):
    """(id, text) of the most specific cited observation about the root cause's component."""
    evidence = {e.get("id"): e for e in record.get("evidence") or []}
    for kind in ("container", "log", "trace"):
        for i in rc.get("evidence") or []:
            e = evidence.get(i) or {}
            if e.get("kind") == kind and e.get("observation"):
                return i, e["observation"][:240]
    return None


def _pad(rows):
    rows = rows[:5]
    for n, row in enumerate(rows, 1):
        row["n"] = n
    if len(rows) < 5:
        rows.append({"n": len(rows) + 1, "why": "Why did that happen?" if rows else "Why did this incident happen?",
                     "because": UNKNOWN_WHY, "evidence": [], "source": "unknown"})
    return rows


# --- went well / could be better -------------------------------------------------------------------

def _fmt_s(s):
    if s is None:
        return "unknown"
    return f"{s:.0f} s" if s <= 120 else f"{s / 60:.1f} minutes"


def assessment(record, times, durs):
    well, better = [], []
    inv, d = record.get("investigation") or {}, record.get("diagnosis") or {}
    plan = record.get("remediation_plan") or {}
    steps = plan.get("steps") or []
    tool_calls = len(inv.get("steps") or [])
    if inv.get("outcome") == "submitted" and d.get("status") == "diagnosed":
        well.append(f"Root cause found in {tool_calls} tool call(s)"
                    + (f", diagnosed {_fmt_s(durs['time_to_diagnose_s'])} after detection"
                       if durs.get("time_to_diagnose_s") is not None else "") + ".")
    elif inv.get("outcome") and inv.get("outcome") != "submitted":
        better.append(f"The investigation ended without a diagnosis ({inv['outcome']}).")
    elif d.get("status") == "escalate":
        better.append(f"Confidence {d.get('confidence')} stayed below the threshold; a human had to take over.")
    cited = sum(1 for e in record.get("evidence") or [] if e.get("cited"))
    if cited:
        well.append(f"The diagnosis cites {cited} evidence item(s) from real telemetry.")
    validation = d.get("validation") or {}
    if validation.get("dropped_evidence_ids"):
        better.append(f"The model cited evidence IDs that do not exist ({', '.join(validation['dropped_evidence_ids'])}); "
                      "the validator dropped them.")
    for o in validation.get("overrides") or []:
        if isinstance(o, dict):
            better.append(f"The validator corrected the model's {o.get('field')}: {o.get('reason')}.")
    ttd = durs.get("time_to_detect_s")
    if ttd is not None:
        (well if ttd <= 120 else better).append(f"Detection took {_fmt_s(ttd)} after the first evidence of the fault.")
    review = record.get("review")
    if not review:
        better.append("The reviewer did not run (ONCALL_REVIEWER off).")
    elif review.get("status") == "skipped":
        better.append(f"The reviewer was skipped: {review.get('reason')}.")
    elif review.get("verdict") == "agree":
        well.append("The reviewer agreed with the diagnosis.")
    else:
        better.append(f"The reviewer said {review.get('verdict')} (confidence {review.get('confidence_before')} -> "
                      f"{review.get('confidence_after')}).")
    for s in steps:
        name = f"{s.get('action')} on {s.get('target')}"
        v, ex, ap = s.get("verify") or {}, s.get("execution") or {}, s.get("approval") or {}
        if v.get("outcome") == "recovered" and v.get("seconds") is not None:
            well.append(f"{name} verified as recovered within {_fmt_s(v['seconds'])}.")
        elif v.get("outcome") in ("not_recovered", "partial", "inconclusive"):
            better.append(f"{name} verified as {v['outcome']}"
                          + (f"; rollback {s['rollback_result']['status']}" if s.get("rollback_result") else "") + ".")
        if ap.get("status") == "rejected":
            better.append(f"{name} was rejected by the on-call engineer.")
        elif ap.get("status") in ("pending", "awaiting"):
            better.append(f"{name} was not run: nobody approved it.")
        if ex.get("status") == "refused" and "kill switch" in str(ex.get("reason", "")).lower():
            better.append(f"{name} was refused: the kill switch was on.")
        if ex.get("status") == "failed":
            better.append(f"{name} failed: {ex.get('error')}.")
    if plan.get("outcome") == "dry_run":
        better.append("Dry run: the fix was proposed but nothing was executed.")
    for e in record.get("escalations") or []:
        if e.get("source") == "no allowlisted fix" and d.get("status") == "diagnosed":
            better.append(f"No allowlisted action fixes {e.get('fault_class')} on {e.get('target')}; escalated to a human.")
    if durs.get("time_to_recover_s") is not None:
        well.append(f"Recovered and verified {_fmt_s(durs['time_to_recover_s'])} after detection.")
    if not times.get("detected"):
        better.append("The detection time is not in the record (the alert had no detected_at).")
    return well, better


# --- action items -------------------------------------------------------------------------------------

def action_items(record):
    d, out_ = record.get("diagnosis") or {}, record.get("outcome") or {}
    rcs = [rc for rc in d.get("root_causes") or [] if isinstance(rc, dict)]
    comp = max(rcs, key=lambda rc: rc.get("confidence") or 0).get("component") if rcs else None
    priority = SEVERITY_PRIORITY.get(config.CRITICALITY.get(comp or ""), "medium")
    items = []
    for p in out_.get("prevention") or d.get("prevention") or []:
        if not isinstance(p, str) or not p.strip():
            continue
        kind = "detect" if _DETECT.search(p) else "mitigate" if _MITIGATE.search(p) else "prevent"
        items.append({"action": p.strip(), "type": kind, "priority": priority, "owner": "to assign"})
    return items


def build_postmortem(record):
    times = key_times(record)
    durs = durations(times)
    well, better = assessment(record, times, durs)
    return {"draft": True, "times": times, "durations": durs, "impact": impact(record),
            "five_whys": five_whys(record), "went_well": well, "could_be_better": better,
            "action_items": action_items(record)}
