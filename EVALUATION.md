# Evaluation note

What we measured for Incident Zero, and what we did not. All live runs used the OpenTelemetry demo
on one laptop, with Claude Opus 5.5 as the model, on 23 and 24 Oct 2026. Faults were injected with
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

## Other checks

- **Fault injection and reset:** all 26 `faultctl` faults were injected for 60 s and reset. Containers
  and shop APIs recovered 26 of 26. Telemetry returned near its healthy snapshot in 25 of 26; the
  exception was payment traffic after `stop-payment`, where the snapshot itself was a burst
  ([results/recovery_audit.json](results/recovery_audit.json)).
- **Automated tests:** 409 pass, using no Docker or network. They cover the flag guards, evidence
  validator, approval rules, kill switch, Action API refusals, rollback and verification.
- **Rule compliance:** a guard blocks flag URLs, ports and files, and flag data is stripped from all
  telemetry. A test confirms the agent never issues state-changing docker commands.

## Limits

- The full loop has only been proven live on one fault type and one action (`restart_service`).
- The load-generator throttle has not been exercised in a saved live run.
- `toggle_feature_flag` and `scale_service` are not implemented (they return 501). Faults that need
  them are escalated rather than fixed.
- Memory leak, CPU pressure, network latency and combined faults were not evaluated end to end in
  the saved runs.
- Diagnosis was measured on a small sample of faults we injected ourselves, so these numbers do not
  predict accuracy on unseen incidents.
- Groq and Gemini support exist, but all results above come from Claude. Accuracy on other models has
  not been measured.
- The incident schema is our own, as the organisers' schema was not available when it was written.
