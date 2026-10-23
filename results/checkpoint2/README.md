# Checkpoint 2: live diagnoses (23 Oct 2026)

Each run: `python scripts/demo.py <fault>` on the live demo (full stack, Locust load).
faultctl injects the fault, the agent waits 180 s and then receives only the alert that
`detector.py` raises from telemetry, never the fault name, and the fault is reset afterwards.
The full run records (tool calls, evidence registry, diagnosis) are the JSON files here.

| Fault injected | Alert (detector.py) | Root cause (agent) | Fault class | Confidence | Proposed action (dry run) | Victims named | Invented evidence |
|---|---|---|---|---|---|---|---|
| stop-payment | checkout error rate 100%, email and payment silent | payment | container_stopped_or_restarting | 0.85 | restart_service payment | checkout, email | none |
| cache-down | checkout, email and others silent | valkey-cart | dependency_down | 0.85 | restart_service valkey-cart | cart, checkout, email, frontend, payment, quote, shipping | none |
| db-down | product-catalog error rate 100% | astronomy-db | dependency_down | 0.92 | restart_service astronomy-db | product-catalog, recommendation, checkout, frontend, payment | none |

- Every evidence ID in each diagnosis exists in that run's registry; the validator dropped nothing.
- All three are `diagnosed` (a root cause with valid evidence and confidence of at least 0.6). Below
  that, the agent escalates to a human.
- Approval gate: these unattended runs had no terminal, so the gate recorded `rejected` (no
  explicit yes). Nothing is executed on Day 1; every proposed action is a dry run.
- Each run took 26 to 34 s and 4 to 6 tool calls.
- Output format: our own diagnosis JSON. The organisers' template schema has not been received
  yet; mapping to it is a single function once it arrives.
