# Incident Zero: status at end of Day 1 (Fri 23 Oct 2026, 20:52)

Measured against the organisers' Day 1 milestones (CLAUDE.md section 6). Everything below was
checked live on this laptop against the full demo stack (core + observability, 25 containers,
Locust load) unless marked otherwise.

## Summary

| Milestone | Result |
|---|---|
| Setup | PASS (with caveats: see "Repo and team") |
| Checkpoint 1: Eyes | PASS |
| Checkpoint 2: Brain | PASS on our own injected incidents. The organisers' JSON schema and practice incidents were never received |
| Day 1 minimum: "an agent that uses tools and correctly diagnoses practice incidents with real evidence" | PASS: 3 of 3 live diagnoses correct, all evidence valid |

**Before anything else:** a real Anthropic API key is still committed in `.env.example` on
`team/main` (commit `d8a7bc2`). It must be revoked and that line emptied (see Known issues #1).

---

## 1. Git

- Branch: `deepthi/checkpoint1`, tracking `team/deepthi/checkpoint1`. **Every commit is pushed**;
  nothing is committed only locally. No stashes.
- Local only (uncommitted):
  - `.env.example`: an editor added a trailing line of spaces (no key)
  - `DEMO_AND_PRESENTATION_GUIDE.pdf` and `Demo_&_Presentation_Guide.pdf`: presentation guides
    (not created by Deepthi's Claude Code session)
  - `scripts/generate_guide_pdf.py`: the script that generated those PDFs
  - `runs/recovery_audit.log`: console log of the recovery audit (its results are committed as
    `results/recovery_audit.json`)
- `team/main` (`c9a72a8`) contains everything up to `79ba6a5`, merged via PR #1, plus
  Aniruddha's `d8a7bc2`. **The 4 Checkpoint 2 commits are only on the branch:** `abc3d57`
  (diagnosis), `24ce810` (faultctl fixes and recovery audit), `fba8d3c`, `fc9582e` (Checkpoint 2
  results).
- `team/shivakumar/checkpoint1` holds Shivakumar's 2 commits (`0c5c0cf` Gemini adapter, root cause,
  approval gate; `ced9179` Jaeger v3). They are not on `main` and will conflict with ours in
  `oncall/agent/run.py`, `oncall/agent/summary.py` (we removed it) and `oncall/sources/live.py`.
- Authors:
  - `team/main`: Deepthi 21, Aniruddha 3 (two e-mail addresses)
  - `deepthi/checkpoint1`: Deepthi 25, Aniruddha 1
  - `shivakumar/checkpoint1`: Shivakumar 2 (plus shared history)
- No `Co-Authored-By` or `Claude-Session` lines anywhere on `team/main`,
  `team/deepthi/checkpoint1` or `team/shivakumar/checkpoint1`.
- `git log --oneline -30` (newest first): `fc9582e` Checkpoint 2 results · `fba8d3c` faultctl
  orphan-loop fix · `24ce810` faultctl recovery fixes and audit · `abc3d57` Checkpoint 2
  diagnosis · `79ba6a5` one detector · `c94cc32` OpenSearch logs and flag stripping · `a816a26`
  faultctl · `419db9b` CLAUDE.md verified facts · `7ee9502` live check fixes · `8a85427` Jaeger v3
  · `8658994` traffic.py · `d2beeec` detector fixes · `d2fbd43` LF normalisation · `a1788b7`
  .env loading · `99cbcf6` merge team/main · `3f08dff` no-attribution rule · `a410c92`
  checkpoint0 (Aniruddha) · `86b1c44` test source · `2a79eb9` architecture · `e6ab6fd` demo.py ·
  `722fcba` incident.py · `8b2e494` agent run · `2a322ad` tools and fixtures · `2ff5cd5`
  expected telemetry · `439ddd7` LLM check · `ee394bb` scaffold.

## 2. Milestones

### Setup (12:00–13:00): PASS with caveats

| Item | Result | Proof |
|---|---|---|
| Demo running with load | PASS | 25 containers Up, 0 unhealthy; `python detector.py --probe`: every service has non-zero rps, `breaches now: 0`; `curl localhost:8080/` gives 200 |
| faultctl injects and resets | PASS | `python -m faultctl list` shows 26 faults; the recovery audit injected and reset all 26 (section 3) |
| LLM key works | PASS (key is compromised) | `python scripts/llm_check.py` gives `OK model=claude-opus-5-5 tool=get_service_health`. The key came from the leaked `team/main` commit |
| Repo forked | PARTIAL | Team repo `aniruddhajoshi100/ocAgent` is in use. The organisers' template repo (skeleton, schema, README template, team form) was never received |
| All 3 members committed | PASS (not all on main) | Deepthi and Aniruddha on `main`; Shivakumar on `shivakumar/checkpoint1` only |

### Checkpoint 1, Eyes (15:30): PASS

| Item | Result | Proof |
|---|---|---|
| Tools: metrics, traces, logs, deploy history | PASS | `get_service_health`/`get_metric` (Prometheus span metrics), `find_error_traces` (Jaeger 2.21 API v3), `search_logs` (OpenSearch with docker logs fallback), `get_deploy_history` (docker inspect/stats). Each was run by CLI during a live stop-payment incident |
| Agent summarises one incident | PASS | `runs/20261023-163510.json`: detector alert `checkout error_rate 1.0, email silent, payment silent`; origin payment (0.9); email named as a downstream victim; every evidence ID valid |
| 1-page architecture sketch | PARTIAL | `ARCHITECTURE.md` shows detector, agent loop, four tools, evidence registry, `submit_diagnosis` and the approval step. It does not show faultctl in the diagram, `traffic.py`, the Jaeger v3 adapter, OpenSearch logs or feature-flag stripping |

### Checkpoint 2, Brain (18:30): PASS on our own incidents

| Item | Result | Proof |
|---|---|---|
| Root cause with evidence and confidence | PASS | `oncall/agent/diagnosis.py`: `root_causes[]` with component, fault_class, cause, confidence, evidence; validator drops uncited claims; status `diagnosed` (confidence >= 0.6) or `escalate` |
| Output in a JSON schema | PARTIAL | Schema: `submit_diagnosis.input_schema` in `oncall/agent/diagnosis.py`. All 8 saved diagnoses validate against it (jsonschema Draft 2020-12, after removing the fields the validator adds). The two 16:18/16:35 runs are Checkpoint 1 summaries in the older format. **The organisers' schema is unknown** |
| Correct on at least 3 practice incidents | PASS (our incidents) | See the table below. The organisers' 10 practice incidents were never received, so these are faultctl faults |

Every agent run (from `runs/*.json`):

| Run | Source | Incident (injected) | Alert signal (detector) | Root cause found | Confidence | Correct | Evidence valid | Time |
|---|---|---|---|---|---|---|---|---|
| 16:18 | live | stop-payment | frontend p95 (old watcher) | payment (summary) | 0.80 | yes | yes | 34.0 s |
| 16:35 | live | stop-payment | checkout error_rate, email/payment silent | payment (summary) | 0.90 | yes | yes | 23.9 s |
| 17:18 | test | payment_down fixture | fixture alert | payment, container_stopped_or_restarting | 0.92 | yes | yes | 21.1 s |
| 17:23 | test | payment_down fixture | fixture alert | payment, container_stopped_or_restarting | 0.92 | yes | yes | 105.3 s |
| **17:53** | live | **stop-payment** | checkout error_rate, email/payment silent | **payment, container_stopped_or_restarting** | 0.85 | **yes** | yes | 26.5 s |
| **18:00** | live | **cache-down** | checkout/email/payment/quote/shipping silent | **valkey-cart, dependency_down** | 0.85 | **yes** | yes | 29.4 s |
| **18:06** | live | **db-down** | product-catalog error_rate, several silent | **astronomy-db, dependency_down** | 0.92 | **yes** | yes | 34.3 s |

Earlier runs (14:04–15:39) ended with `llm_error` because no key was set. Bold rows are the
Checkpoint 2 evidence (`results/checkpoint2/`). Each proposed a dry-run `restart_service` of the
failed component.

## 3. Recovery (`faultctl reset`)

Recovery audit (`scripts/recovery_audit.py`, `results/recovery_audit.json`, 16:40–17:50): every
one of the 26 faults was injected for 60 s, then reset.

- Container recovered: **26/26**, all within 61 s.
- Dependents (all public shop APIs return 200): **26/26**, all within 39 s.
- Telemetry back near the healthy snapshot (target and its callers): **25/26**, slowest 254 s
  (mem-starve-product-catalog).
- Not fully recovered: `stop-payment` telemetry only. Payment ran at 0.047 rps against a 0.111 rps
  snapshot, which was a burst; normal is 0.05–0.06. This is threshold noise, and the other three
  payment faults recovered.
- Restart loops did not crash `payment`, `checkout` and `product-catalog` as intended: PID 1
  ignored SIGTERM, or the image has no shell. **Fixed in `24ce810`**: the crash is now verified and
  falls back to kill + start (verified live on payment). Docker does not count these as restarts,
  so RestartCount stays 0 for those three.
- Also fixed: reset now kills orphaned restart-loop processes, restores limits from
  `faultctl/originals.json` if `state.json` is lost, and starts every stopped demo container.
  `reset` twice gives "nothing to reset" both times.
- Expected side effects: restart counts rose for the six targets (restart loops, memory
  starvation); every container ended running and healthy.

## 4. Rules

| Check | Result |
|---|---|
| Demo folder unchanged | PASS: `git -C C:/Placement/opentelemetry-demo status --short` is empty |
| No flag reads in agent code | PASS: no flag URLs, ports or files outside the guard's own forbidden list; flagd containers are never listed; flagd spans and `feature_flag.*` data are stripped from traces, metrics, logs and bundles |
| Isolation / flag / guard tests | PASS: test_isolation 26, test_sanitize_opensearch 5, test_jaeger_v3 4, test_foundations 15, test_source_modes 6 |
| No secrets tracked | PASS on our branch: no `.env`, state, originals, incidents or baseline files tracked; no key patterns in tracked files; `.env` gitignored. **FAIL on `team/main`**: the key is in `.env.example` |
| Full test suite (local working copy) | **106 passed, 0 failed** |

## 5. Known issues, in priority order

1. **Leaked API key on `team/main`** (`d8a7bc2`). Revoke and replace it, then commit an empty
   `ANTHROPIC_API_KEY=` to `.env.example` on `main`. History keeps the key, so revoking is the
   real fix.
2. **Checkpoint 2 is not on `main`.** Merge `deepthi/checkpoint1` (4 commits) after issue 1.
3. **Shivakumar's branch conflicts** with ours (`run.py`, `summary.py`, `live.py`; a Gemini
   adapter and a second approval gate). Decide what to keep and merge it deliberately.
4. **Organisers' inputs are missing:** template repo, JSON schema, alert format, Action API, the 10
   practice incidents, deploy-history source. Ask the mentors first thing on Day 2.
5. **Approval gate in unattended runs** records `rejected` (no terminal, no explicit yes). Safe, but
   run the demo in a terminal and answer `y`/`n`.
6. **Restart-loop signature:** for payment, checkout and product-catalog the loop is kill + start,
   so RestartCount stays 0. The agent still sees recent restarts and exits.
7. **Telemetry gaps:** checkout does not log to stdout, and logs nothing at warn level or above
   in OpenSearch; Envoy access logs have no severity, so they count as info; under Locust load,
   latency baselines are high (checkout p95 limit 9100 ms), so latency faults need a big change to
   alert.
8. **ARCHITECTURE.md** is missing faultctl (diagram), `traffic.py`, Jaeger v3, OpenSearch and flag
   stripping.
9. **Untracked local files** (PDFs, `scripts/generate_guide_pdf.py`): decide whether they belong
   in the repo.

## 6. Day 2 readiness (Checkpoint 3 at 12:00, code freeze at 14:00)

| Day 2 item | Exists | Missing |
|---|---|---|
| Action API client, allowlisted actions | Allowlist in `diagnosis.py` (`restart_service`, `toggle_feature_flag`, `throttle_load_generator`, `scale_service`, `escalate_to_human`, `none`); `ACTION_API_URL` placeholder in config | The client itself. Needs the organisers' Action API endpoints and auth |
| Approval gate | `request_human_approval()`: prompts for an explicit y/N, pending when unattended, `--auto-approve` flag | Wiring to real execution; recording who approved |
| Execute | none | Calls through the Action API only (HARD RULE 1); every call logged to `runs/actions.jsonl` (already read by `get_deploy_history`) |
| Verify recovery, roll back, escalate | detector baselines, `get_service_health`, recovery audit logic | Post-action check against baseline with a timeout; rollback and escalate on failure |
| Dry run | All proposed actions forced to `dry_run: true` | A dry-run path through the Action API, if it offers one |
| Kill switch | `python -m oncall stop` / `resume` (STOP file checked before every model and tool call) | Also checking it before any action call |
| Incident report | Run records with timeline data (alert, tool calls, evidence, diagnosis) | A report generator: timeline, root cause, evidence, action, prevention |
| README | none (template not received) | Write from the template, or our own |
| Evaluation note | none | Results table (this file and `results/`) can seed it |
| Team form | none (template not received) | Get it from the organisers |

Suggested order for Day 2 morning: fix the key and merge to main → get the Action API and schema
from the mentors → Action API client with logging → approve → execute → verify → report, for one
fault end to end (Checkpoint 3) → README, evaluation note, team form → clean run from scratch
before 13:30.

## 7. Shop health at the end of this report

`python -m faultctl reset` gives "nothing to reset", "no faults injected";
`python detector.py --probe` shows `breaches now: 0`; `localhost:8080/` returns 200; 0 demo
containers not Up.
