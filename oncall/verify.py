"""Verify an action (or a human's manual fix) against runs/baseline.json. Read-only.

Signals per poll:
  serving     POSITIVE evidence that the root-cause service works again since the action: a
              successful server span from it in Jaeger, or a successful client call to it from one
              of its callers. Required for "recovered"; absence of errors is never enough.
  fast path   (VERIFY_FAST_PATH) container running and successful server spans of the involved
              services since the action. Reports "recovering" early and polls every
              VERIFY_FAST_INTERVAL_S.
  metrics     calls, errors and latency from span-metric counter increases since the previous
              Prometheus sample (per series, with resets handled like increase()), compared with
              the baseline. A check counts only when a new sample has arrived (about every 60 s).

Outcomes:
    recovered       VERIFY_CONSECUTIVE metrics checks in a row passed AND the root-cause service
                    was seen serving since the action
    partial         at the timeout some judged checks pass and some do not
    not_recovered   at the timeout no judged check passes
    inconclusive    nothing could be judged: no traffic observed, no shop traffic, no baseline or
                    no new metrics (verify["reason"] says which)

Every metrics check, the first "recovering" signal and the serving evidence are registered.
"""
import time
from datetime import datetime, timezone

from oncall import config
from oncall.agent.actions import callers_of
from oncall.evidence import registry
from oncall.sources import SourceError, get_source
from oncall.tools._common import metric_names
from oncall.tools.metrics import compute_health, load_baseline
from oncall.tools.traces import parse_trace

RECOVERED, PARTIAL, NOT_RECOVERED, INCONCLUSIVE = "recovered", "partial", "not_recovered", "inconclusive"
CONTAINER_ACTIONS = ("restart_service", "manual_remediation")
NO_TRAFFIC, NO_SHOP_TRAFFIC = "no traffic observed", "no traffic: shop traffic near zero"


def quantile_from_buckets(q, buckets):
    """Prometheus-style histogram_quantile over {le: count} (cumulative). None if empty."""
    bounds = sorted(buckets.items(), key=lambda kv: float(kv[0]))
    total = bounds[-1][1] if bounds else 0
    if total <= 0:
        return None
    rank, prev_le, prev_count = q * total, 0.0, 0.0
    for le, count in bounds:
        le_f = float(le)
        if count >= rank:
            if le_f == float("inf"):
                return round(prev_le, 1)
            span = count - prev_count
            return round(prev_le + (le_f - prev_le) * ((rank - prev_count) / span if span else 1), 1)
        prev_le, prev_count = le_f, count
    return round(prev_le, 1)


def increases(prev, cur):
    """Per-series counter increase like Prometheus increase(): a drop means the counter was
    reset, so it is counted from zero (the increase is the new value). New series count from zero."""
    out = {}
    for key, value in cur.items():
        before = prev.get(key)
        out[key] = value - before if before is not None and value >= before else value
    return out


def series_key(metric):
    return tuple(sorted((k, v) for k, v in metric.items() if k != "__name__"))


class Verifier:
    def __init__(self, timeout_s=None, interval_s=None, consecutive=None, window=None,
                 fast_path=None, fast_interval_s=None, sleep=time.sleep, clock=time.monotonic):
        self.timeout_s = config.VERIFY_TIMEOUT_S if timeout_s is None else timeout_s
        self.interval_s = config.VERIFY_INTERVAL_S if interval_s is None else interval_s
        self.consecutive = consecutive or config.VERIFY_CONSECUTIVE
        self.window = window or config.VERIFY_WINDOW
        self.fast_path = config.VERIFY_FAST_PATH if fast_path is None else fast_path
        self.fast_interval_s = config.VERIFY_FAST_INTERVAL_S if fast_interval_s is None else fast_interval_s
        self.sleep, self.clock = sleep, clock
        self._ref = None  # counters of the last Prometheus sample already judged

    # --- measurements ----------------------------------------------------------------------
    def measure(self, services):
        """Windowed health right now (the "before" snapshot): {service: row or None}."""
        health = compute_health(self.window)["services"]
        return {s: health.get(s) for s in services}

    def counters(self, services):
        """Raw cumulative server-span counters per series for the given services, plus the time of
        the newest sample: {"t", "calls": {series: v}, "buckets": {series: v}}."""
        names = metric_names()
        calls, dur = names["calls"], names["duration"]
        if not calls:
            raise RuntimeError("no span-metrics calls metric in Prometheus")
        src = get_source()
        sel = (f'{config.SPAN_KIND_LABEL}=~"{"|".join(config.SERVER_KINDS)}",'
               f'{config.SERVICE_LABEL}=~"{"|".join(sorted(set(services)))}"')
        newest = src.prom_query(f"max(timestamp({calls}{{{sel}}}))")
        return {"t": float(newest[0]["value"][1]) if newest else None,
                "calls": {series_key(s["metric"]): float(s["value"][1])
                          for s in src.prom_query(f"{calls}{{{sel}}}")},
                "buckets": {series_key(s["metric"]): float(s["value"][1])
                            for s in src.prom_query(f"{dur}{{{sel}}}")} if dur else {}}

    def sample(self, services):
        """(rows, interval_s, fresh): per-service health over the interval since the previous new
        sample. fresh is False until Prometheus has a sample we have not judged yet."""
        cur = self.counters(services)
        if cur["t"] is None or (self._ref is not None and cur["t"] <= self._ref["t"]):
            return {}, 0.0, False
        if self._ref is None:
            self._ref = cur
            return {}, 0.0, False
        prev, self._ref = self._ref, cur
        dt = max(cur["t"] - prev["t"], 1.0)
        calls, errors, buckets = {}, {}, {}
        for key, inc in increases(prev["calls"], cur["calls"]).items():
            labels = dict(key)
            svc = labels.get(config.SERVICE_LABEL)
            calls[svc] = calls.get(svc, 0.0) + inc
            if labels.get(config.STATUS_LABEL) == config.ERROR_STATUS:
                errors[svc] = errors.get(svc, 0.0) + inc
        for key, inc in increases(prev["buckets"], cur["buckets"]).items():
            labels = dict(key)
            per_le = buckets.setdefault(labels.get(config.SERVICE_LABEL), {})
            per_le[labels.get("le")] = per_le.get(labels.get("le"), 0.0) + inc
        rows = {}
        for svc in services:
            dc, de = calls.get(svc, 0.0), errors.get(svc, 0.0)
            rows[svc] = {"calls": dc, "rps": round(dc / dt, 4),
                         "error_rate": round(de / dc, 4) if dc else 0.0,
                         "p95_ms": quantile_from_buckets(0.95, buckets.get(svc, {})) if dc else None}
        return rows, dt, True

    def container(self, name):
        """Running and not unhealthy, from docker inspect; None if the container is unknown."""
        try:
            info = get_source().docker_inspect([name])
        except SourceError:
            return None
        if not info:
            return None
        st = info[0].get("State", {})
        health = (st.get("Health") or {}).get("Status", "none")
        ok = bool(st.get("Running")) and health != "unhealthy"
        return {"ok": ok, "status": st.get("Status"), "health": health}

    @staticmethod
    def root_of(step):
        """The service that must be seen serving: the root-cause component of the step."""
        root = step.get("component") or step["target"]
        if step["action"] == "throttle_load_generator" and root == step["target"]:
            return config.ENTRY_SERVICES[0]
        return root

    def serving(self, step, since):
        """Positive evidence since `since` (epoch s): successful server spans of the root-cause
        service, or successful client calls to it from its callers (for services without spans,
        e.g. a cache or database)."""
        root, src = self.root_of(step), get_source()
        lookback = max(src.now() - since + 5, config.VERIFY_FAST_LOOKBACK_S)
        found = {"target": root, "server_spans": 0, "client_calls": 0, "example_trace": None}
        try:
            for t in src.jaeger_traces(root, lookback, 20, error_only=False):
                for sp in parse_trace(t).values():
                    if sp["service"] == root and sp["kind"] in ("server", "consumer") \
                            and not sp["error"] and sp["start"] / 1e6 >= since:
                        found["server_spans"] += 1
                        found["example_trace"] = found["example_trace"] or t.get("traceID")
            if not found["server_spans"]:
                for caller in callers_of(root):
                    for t in src.jaeger_traces(caller, lookback, 20, error_only=False):
                        spans = parse_trace(t)
                        children = {}
                        for sp in spans.values():
                            children.setdefault(sp["parent"], []).append(sp)
                        for sp in spans.values():
                            reaches = root.lower() in str(sp["peer"] or "").lower() or any(
                                c["service"] == root for c in children.get(sp["id"], []))
                            if sp["service"] == caller and sp["kind"] == "client" and reaches \
                                    and not sp["error"] and sp["start"] / 1e6 >= since:
                                found["client_calls"] += 1
                                found["example_trace"] = found["example_trace"] or t.get("traceID")
        except SourceError as e:
            found["error"] = str(e)[:200]
        found["ok"] = bool(found["server_spans"] or found["client_calls"])
        return found

    def fast_signal(self, step, services, baseline, since):
        """Container state plus Jaeger server spans of each service since `since` (epoch s).
        Per service: ok (successful spans, no errors), waiting (no spans yet) or failing."""
        result = {"services": {}, "container": None}
        if step["action"] in CONTAINER_ACTIONS:
            result["container"] = self.container(step["target"])
        src = get_source()
        for svc in services:
            base_rps = (baseline.get(svc) or {}).get("rps") or 0.0
            if base_rps < config.SILENT_MIN_BASE_RPS:
                continue  # no spans expected (e.g. a cache): the container decides
            lookback = min(max(config.VERIFY_FAST_LOOKBACK_S, 3 / base_rps), 180)
            try:
                traces = src.jaeger_traces(svc, lookback, 20, error_only=False)
            except SourceError as e:
                result["services"][svc] = {"state": "unknown", "error": str(e)[:120]}
                continue
            spans = [sp for t in traces for sp in parse_trace(t).values()
                     if sp["service"] == svc and sp["kind"] in ("server", "consumer")
                     and sp["start"] / 1e6 >= since]
            bad = sum(1 for sp in spans if sp["error"])
            good = len(spans) - bad
            state = "failing" if bad else "ok" if good else "waiting"
            result["services"][svc] = {"state": state, "ok_spans": good, "error_spans": bad}
        states = [v["state"] for v in result["services"].values()]
        if result["container"] is not None:
            states.append("ok" if result["container"]["ok"] else "failing")
        result["ok"] = bool(states) and all(s == "ok" for s in states)
        return result

    # --- one check -----------------------------------------------------------------------------
    @staticmethod
    def judge(now, base, judge_rps=True):
        """(ok, problems) for one service; ok None when there is nothing to compare with."""
        if not base:
            return None, ["no baseline"]
        base_rps = base.get("rps") or 0.0
        if now is None:
            if base_rps >= config.SILENT_MIN_BASE_RPS:
                return False, ["no traffic"]
            return None, ["idle in baseline and now"]
        problems = []
        rps = now.get("rps") or 0.0
        if judge_rps and base_rps >= config.SILENT_MIN_BASE_RPS and rps < config.VERIFY_RPS_MIN_RATIO * base_rps:
            problems.append(f"rps {rps} < {config.VERIFY_RPS_MIN_RATIO:.0%} of baseline {base_rps}")
        if judge_rps and base_rps and rps > config.VERIFY_RPS_MAX_RATIO * base_rps:
            problems.append(f"rps {rps} > {config.VERIFY_RPS_MAX_RATIO}x baseline {base_rps}")
        err, base_err = now.get("error_rate") or 0.0, base.get("error_rate") or 0.0
        if err > base_err + config.VERIFY_ERR_MARGIN:
            problems.append(f"error rate {err:.1%} > baseline {base_err:.1%} + {config.VERIFY_ERR_MARGIN:.0%}")
        p95, base_p95 = now.get("p95_ms"), base.get("p95_ms")
        if p95 is not None and base_p95 and p95 >= config.VERIFY_P95_FLOOR_MS \
                and p95 > config.VERIFY_P95_MAX_RATIO * base_p95:
            problems.append(f"p95 {p95}ms > {config.VERIFY_P95_MAX_RATIO}x baseline {base_p95}ms")
        return not problems, problems

    def check(self, step, services, baseline, n):
        """One metrics check over the latest interval (or {'fresh': False} if no new sample yet)."""
        entries = [e for e in config.ENTRY_SERVICES if e not in services]
        try:
            rows, dt, fresh = self.sample(list(services) + entries)
        except (SourceError, RuntimeError) as e:
            return {"poll": n, "fresh": False, "error": str(e)[:200]}
        if not fresh:
            return {"poll": n, "fresh": False}
        result = {"poll": n, "fresh": True, "interval_s": round(dt, 1), "services": {}, "container": None,
                  "no_shop_traffic": False}

        base_shop = sum((baseline.get(e) or {}).get("rps") or 0.0 for e in config.ENTRY_SERVICES)
        shop = sum((rows.get(e) or {}).get("rps") or 0.0 for e in config.ENTRY_SERVICES)
        if base_shop >= config.SILENT_MIN_BASE_RPS and shop < config.VERIFY_SHOP_MIN_RATIO * base_shop:
            # Nothing is being exercised, so "no errors" means nothing: judge no service.
            result.update(no_shop_traffic=True, judged=0, passed=0, ok=False,
                          services={s: {"ok": None, "problems": ["no shop traffic"], "now": rows.get(s)}
                                    for s in services})
            result["evidence"] = registry.add(
                "verify", "metric", f"verify {step['action']} {step['target']} check {n}: no shop traffic "
                f"(entry rps {shop:.3f} vs baseline {base_shop:.3f}); nothing can be judged", result)
            return result

        for svc in services:
            row, base = rows.get(svc), baseline.get(svc)
            expected = ((base or {}).get("rps") or 0.0) * dt
            if row is not None and row.get("calls") == 0 and expected < config.VERIFY_MIN_EXPECTED_CALLS:
                ok, problems = None, [f"too little traffic to judge (expected {expected:.1f} calls)"]
            else:
                ok, problems = self.judge(row, base, judge_rps=expected >= config.VERIFY_MIN_EXPECTED_CALLS)
            result["services"][svc] = {"ok": ok, "problems": problems, "now": row}
        if step["action"] in CONTAINER_ACTIONS:
            result["container"] = self.container(step["target"])
        verdicts = [v["ok"] for v in result["services"].values() if v["ok"] is not None]
        if result["container"] is not None:
            verdicts.append(result["container"]["ok"])
        result["judged"] = len(verdicts)
        result["passed"] = sum(1 for v in verdicts if v)
        result["ok"] = bool(verdicts) and all(verdicts)
        bad = [f"{s}: {'; '.join(v['problems'])}" for s, v in result["services"].items() if v["ok"] is False]
        if result["container"] and not result["container"]["ok"]:
            bad.append(f"container {step['target']} {result['container']['status']}/{result['container']['health']}")
        text = (f"verify {step['action']} {step['target']} check {n} ({result['interval_s']}s of new data): "
                f"{'healthy' if result['ok'] else 'not healthy'} ({result['passed']}/{result['judged']} ok)"
                + (f"; {' | '.join(bad)}" if bad else ""))
        result["evidence"] = registry.add("verify", "metric", text, result)
        return result

    # --- the loop ----------------------------------------------------------------------------------
    def verify(self, step, services, before=None, progress=None):
        say = progress or (lambda s: None)
        baseline = (load_baseline() or {}).get("services", {})
        since = get_source().now()
        start, checks, streak, quiet, polls = self.clock(), [], 0, 0, 0
        recovering, fast, serving, reason = None, None, None, None
        self._ref = None
        while True:
            polls += 1
            elapsed = self.clock() - start
            if self.fast_path:
                try:
                    fast = self.fast_signal(step, services, baseline, since)
                except Exception as e:  # the fast path is a hint; never let it break verification
                    fast = {"ok": False, "error": str(e)[:200]}
                if fast.get("ok") and recovering is None:
                    recovering = {"at_s": round(elapsed, 1), "evidence": registry.add(
                        "verify", "trace", f"verify {step['action']} {step['target']}: RECOVERING after "
                        f"{elapsed:.0f}s (container running, server spans succeed, no error spans)", fast)}
                    say(f"[verify] {step['target']}: recovering after {elapsed:.0f}s; waiting for metrics")
            if serving is None:
                s = self.serving(step, since)
                if s["ok"]:
                    s["at_s"] = round(elapsed, 1)
                    s["evidence"] = registry.add(
                        "verify", "trace", f"verify {step['action']} {step['target']}: {s['target']} is serving "
                        f"again: {s['server_spans']} successful server span(s), {s['client_calls']} "
                        f"successful call(s) to it since the action (e.g. trace {s['example_trace']})", s)
                    serving = s
                    say(f"[verify] {s['target']}: serving again after {elapsed:.0f}s")

            c = self.check(step, services, baseline, len(checks) + 1)
            c["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            c["serving_seen"] = serving is not None
            c["fast_ok"] = (fast or {}).get("ok") if self.fast_path else None
            if c["fresh"] or c.get("error"):
                checks.append(c)
            if c["fresh"]:
                if c["no_shop_traffic"]:
                    quiet += 1
                    say(f"[verify] {step['target']}: no shop traffic, nothing to judge ({quiet}/{self.consecutive})")
                else:
                    quiet = 0
                    streak = streak + 1 if c["ok"] else 0
                    say(f"[verify] {step['target']}: metrics check {len(checks)} "
                        f"{'healthy' if c['ok'] else 'not healthy'} ({streak}/{self.consecutive})")
            if streak >= self.consecutive and serving:
                outcome = RECOVERED
                break
            if quiet >= self.consecutive:
                outcome, reason = INCONCLUSIVE, NO_SHOP_TRAFFIC
                break
            if c["fresh"] and not baseline and not c["judged"]:
                outcome, reason = INCONCLUSIVE, "no baseline"
                break
            if self.clock() - start >= self.timeout_s:
                outcome, reason = self._final([x for x in checks if x["fresh"]], serving)
                break
            self.sleep(self.fast_interval_s if self.fast_path else self.interval_s)

        fresh = [x for x in checks if x["fresh"]]
        last = fresh[-1] if fresh else {"services": {}, "container": None}
        return {
            "outcome": outcome, "reason": reason, "baseline": "loaded" if baseline else "missing",
            "seconds": round(self.clock() - start, 1), "polls": polls,
            "recovering": recovering, "serving": serving, "fast_path": fast,
            "checks": [{**{k: c.get(k) for k in ("poll", "at", "ok", "passed", "judged", "interval_s",
                                                  "no_shop_traffic", "serving_seen", "fast_ok",
                                                  "container", "evidence", "error")},
                        "signals": {s: {"ok": v["ok"], "problems": v["problems"]}
                                    for s, v in (c.get("services") or {}).items()}} for c in checks],
            "services": {s: {"baseline": baseline.get(s), "before": (before or {}).get(s),
                             "after": last["services"].get(s, {}).get("now"),
                             "ok": last["services"].get(s, {}).get("ok"),
                             "problems": last["services"].get(s, {}).get("problems", [])}
                         for s in services},
            "container": last.get("container"),
            "unhealthy": [s for s, v in last["services"].items() if v["ok"] is False],
            "evidence": [e["evidence"] for e in (recovering, serving) if e]
                        + [c["evidence"] for c in checks if c.get("evidence")],
        }

    @staticmethod
    def _final(fresh, serving):
        """(outcome, reason) at the timeout. Failing checks are real (negative) evidence; healthy
        checks without positive evidence of serving are not."""
        if not fresh:
            return INCONCLUSIVE, "no new metrics"
        last = fresh[-1]
        if last["no_shop_traffic"]:
            return INCONCLUSIVE, NO_SHOP_TRAFFIC
        if not last["judged"]:
            return INCONCLUSIVE, NO_TRAFFIC
        if last["passed"] == 0:
            return NOT_RECOVERED, None
        if last["passed"] == last["judged"]:  # healthy, but not confirmed
            return INCONCLUSIVE, (NO_TRAFFIC if not serving else
                                  "healthy checks were not consecutive before the timeout")
        return PARTIAL, None
