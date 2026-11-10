# Evaluation note

What we measured for Incident Zero, and what we did not. All live runs used the OpenTelemetry demo
on one laptop, with Claude Opus 5.5 as the model, on 23 and 24 Oct 2026. Later offline runs on other
models have their own section. Faults were injected with
our own `faultctl`, because the organisers' ten practice incidents never reached us. Results from
the organisers' hidden test are not available to us, so none are claimed here.

The saved outputs are in [results/checkpoint2/](results/checkpoint2/) and
[results/day2/](results/day2/).

## Summary

| Question | Result |
|---|---|
| Does it diagnose real faults with real evidence? | Yes, on 3 of 3 Checkpoint 2 faults, with every evidence ID valid |
| Does the full loop work (alert, diagnose, approve, act, verify, report)? | Yes, live, 3 times, all for the same fault: a stopped payment service |
| Does it stop when told to? | Yes: the kill switch refused an already-approved step |
| Does it escalate when it should? | Yes, when confidence was low or no allowlisted fix existed |
| Has rollback run live? | No. It is covered by automated tests only |

## Diagnosis (live, Day 1)

| Injected fault | Root cause found | Fault class | Confidence | Time | Correct |
|---|---|---|---|---|---|
| stop-payment | payment | container_stopped_or_restarting | 0.85 | 26.5 s | Yes |
| cache-down | valkey-cart | dependency_down | 0.85 | 29.4 s | Yes |
| db-down | astronomy-db | dependency_down | 0.92 | 34.3 s | Yes |

## Full loop and safety (Day 2)

| Run | Mode | Diagnosis | Confidence | Outcome | Verification | Estimated LLM cost |
|---|---|---|---|---|---|---|
| 20261024-105640 | live, executed | payment stopped | 0.92 | Recovered | 175.5 s | $0.19 |
| 20261024-121431 | live, executed | payment stopped | 0.92 | Recovered | 227.1 s | $0.14 |
| 20261024-141335 | live, executed | payment stopped | 0.92 | Recovered | 279.8 s | $0.17 |
| 20261024-110614 | live, kill switch on | payment stopped | 0.90 | Step refused, escalated | Not run | $0.25 |
| 20261024-160811 | live, dry run | payment stopped | 0.92 | One step proposed, nothing executed | Skipped | $0.20 |
| 20261024-142806 | live, manual alert | payment 0.65, ad 0.20 | see left | No allowlisted action, escalated | Not run | $0.44 |
| 20261024-154738-test | offline fixture | product-catalog, one product | 0.85 | No allowlisted action, escalated | Not run | $0.14 |

Every executed restart in these runs had an explicit human approval, recorded in the run file. The
Action API's own audit log holds 5 applied `restart_service` calls in total, all for payment
([results/day2/action_api.actions_only.jsonl](results/day2/action_api.actions_only.jsonl)). Two of
those belong to runs that were interrupted and are not counted as recoveries above.

## Other models (offline fixtures, 8 Nov 2026)

These runs used synthetic fixtures, with nothing executed, and are a small sample. Saved outputs are
in [results/groq_ollama/](results/groq_ollama/). Groq means `openai/gpt-oss-120b` on a tier limited
to 8,000 tokens per minute; Ollama means `qwen3:8b` on the local GPU.

| Run | Fixture (actual fault) | Model | Result | Time |
|---|---|---|---|---|
| 20261108-152959 | payment_down (container stopped) | Groq | Correct, 0.75 | 105 s |
| 20261108-153656 | payment_app_error (Charge errors) | Groq | Correct, 0.77 | 97 s |
| 20261108-153148 | product_one_item (one product fails) | Groq | No diagnosis: malformed tool-call JSON, since retried | 174 s |
| 20261108-153455 | homepage_flood (traffic flood) | Groq | No diagnosis: request above the tier's limit | 112 s |
| 20261108-161752 | homepage_flood | Groq and Ollama | Correct class, 0.5 after review, escalated | 531 s |
| 20261108-162654 | product_one_item | Groq and Ollama | **Wrong**: `service_error` at 0.95, proposed restarting `astronomy-db` | 336 s |
| 20261108-164552 | product_one_item | Ollama only | Correct class (`input_specific_error`), 0.95, no fix offered | 225 s |

The wrong diagnosis led to a rule: a diagnosis the fallback helped produce keeps its findings but
offers no fix, and goes to a human. Run 20261108-162654 predates that rule. The same fixture gave
different answers on different runs, so these confidence numbers should not be read as reliable.

## Several faults at once and unseen faults (offline fixtures, 9 Nov 2026)

Synthetic fixtures, nothing executed. Saved outputs are in [results/multi_fault/](results/multi_fault/)
and [results/unseen/](results/unseen/).

| Run | Fixture (actual fault) | Model | Result | Time |
|---|---|---|---|---|
| 20261109-084014 | two_app_faults (payment errors + one product fails) | Groq | Both causes found: payment `service_error` 0.94, product-catalog `input_specific_error` 0.92; escalated, as neither has an allowlisted fix | 193 s |
| 20261109-110811 | unseen_disk_full (Postgres out of disk) | Groq | Right origin (astronomy-db); the reviewer doubted "disk full" against a quota, lowering 0.59 to 0.39; escalated | 183 s |
| 20261109-115221 | unseen_cert_expired (TLS to currency) | Ollama | Right cause (expired certificate on the currency call), 0.95; its proposed restart was replaced by escalation by the validator | 137 s |

## Totals across all saved runs (counted 10 Nov 2026)

Counted from all 55 run records in `results/`. Runs that were not already in another folder are in
[results/all_runs/](results/all_runs/), with email addresses redacted. Four runs from 23 Oct ended
with no diagnosis because no key was set; they are counted as runs but not as diagnoses.

| Measure | Count |
|---|---|
| Saved runs / live runs | 55 / 26 |
| Diagnoses with root causes / root-cause claims / evidence citations | 30 / 38 / 148 |
| Citations the validator dropped as not collected | 0 |
| Live Claude runs on 24 Oct naming payment's stopped container as the top cause | 15 of 15; confidence 0.65 to 0.92, median 0.90 |
| Of those, runs where payment itself was not in the alert | 2 (20261024-160811, 20261024-163644), plus 1 manual alert |
| Diagnosis time, those 15 runs | 28.9 to 61.6 s, median 32.5 s; 3 to 7 tool steps, median 3 |
| Model cost, those 15 runs | $0.14 to $0.44, median $0.19 |
| Proposals rejected by a person / dry runs / executed and recovered | 4 / 4 / 3 |
| Validator changed the proposed action | 5 runs: 3 restarts became escalations (20261109-115221, 20261109-122037, 20261109-171801), 1 escalation became a throttle for a traffic flood, 1 restart was retargeted |

The 15 repeat runs were practice runs of the stopped-payment incident. Which fault was injected was
not recorded in the run files; the alerts (payment silent, checkout errors) and the Day 2 notes match it.

## Improvement over time

| Measure | 23 Oct 20:53 (end of Day 1) | 24 Oct 13:57 (code freeze) | 24 Oct 16:19 | 8 Nov | 9 Nov |
|---|---|---|---|---|---|
| Tests collected by pytest | 106 | 301 | 406 | 484 | 616 |
| Recorded incident fixtures | 1 | 4 | 4 | 4 | 7 |

Counted with `pytest --collect-only` at each commit of `main`.

## Other checks

- **Fault injection and reset:** all 26 `faultctl` faults were injected for 60 s and reset. Containers
  and shop APIs recovered 26 of 26. Telemetry returned near its healthy snapshot in 25 of 26; the
  exception was payment traffic after `stop-payment`, where the snapshot itself was a burst
  ([results/recovery_audit.json](results/recovery_audit.json)).
- **Automated tests:** 616 pass, using no Docker or network. They cover the flag guards, evidence
  validator, approval rules, kill switch, Action API refusals, rollback and verification.
- **Rule compliance:** a guard blocks flag URLs, ports and files, and flag data is stripped from all
  telemetry. A test confirms the agent never issues state-changing docker commands.

## Limits

- The full loop has only been proven live on one fault type and one action (`restart_service`).
- The load-generator throttle has not been exercised in a saved live run.
- `toggle_feature_flag` and `scale_service` are not implemented (they return 501). Faults that need
  them are escalated rather than fixed.
- Memory leak, CPU pressure, network latency and combined faults were not evaluated end to end in
  the saved live runs. Combined and unseen faults have only been run on offline fixtures, one run each.
- Diagnosis was measured on a small sample of faults we injected ourselves, so these numbers do not
  predict accuracy on unseen incidents.
- All live results come from Claude. Groq and Ollama have only run on offline fixtures, and Gemini has
  not been evaluated.
- On Groq's 8,000 tokens-per-minute tier, some requests exceed the limit outright and need the fallback.
- The incident schema is our own, as the organisers' schema was not available when it was written.
