Never add "Co-Authored-By" or "Claude-Session" lines to commit messages.

# Incident Zero – AI On-Call Engineer
Bosch one-day AI hackathon at PES University (Day 1: Fri 23 Oct 2026, Day 2: Sat 24 Oct 2026).
Theme: Agentic AI for IT Operations, from alert to a safe, approved fix.

This file is the team's single source of truth. Claude Code: read it fully before every task and
follow the HARD RULES without exception.

---

## 1. Problem statement
Build an AI On-Call Engineer: an agent that responds to a production incident in a cloud-native
application, investigates it using real tools, identifies the root cause with evidence, and
resolves it safely with human approval.

The real-world pain it automates: when production breaks (often at 2 a.m.), an on-call engineer reads
logs, checks dashboards, looks at recent deploys, guesses the cause and applies a fix under pressure.
Most of that first hour is searching and correlating. The agent does this first hour:
receive alert → decide which tools to call and in what order → reason over logs, metrics, traces and
deployment history → name the root cause with the evidence behind it → propose a fix from an approved
runbook → ask a human for approval → apply it → check the system actually recovered.

Guiding principle (from organisers): "A good agent is not the one that always acts. It is the one that
acts when it has evidence, and says 'I am not sure, here is what I found' when it does not."
When not confident, escalating to a human with findings is a CORRECT outcome and SCORES BETTER than a
confident wrong fix.

## 2. What the solution must do (7 steps, as specified by organisers)
1. **Detect the incident.** Receive an alert (an error-rate or latency threshold on the demo's metrics)
   and start an investigation automatically. A simple threshold watcher is enough; no LLM is needed here.
2. **Investigate with tools.** Tools to query traces, metrics, logs and container health. The agent
   decides which tool to call next based on what it has found so far, rather than pasting all the data
   into one prompt.
3. **Diagnose with evidence.** Name the failing component and the cause, with a confidence level and the
   exact traces, log lines or metric values that support it. Evidence must exist in the data; invented
   evidence is penalised.
4. **Propose a safe fix.** Pick a remedy only from the allowlisted runbook actions in the Action API
   (restart a service, toggle a feature flag, throttle the load generator, scale a service on
   Kubernetes), with a short reason.
5. **Ask for human approval.** Show the diagnosis and plan to a human and wait. Nothing changes without
   an explicit yes. Provide a dry-run mode and a kill switch.
6. **Act and verify.** Execute the approved action, then check health metrics to confirm the system
   recovered. If it did not, roll back and escalate.
7. **Report.** Produce a short incident report: timeline, root cause, evidence, action taken, and what
   would prevent a repeat.

## 3. HARD RULES (out of scope; violating any of these risks disqualification)
1. NEVER edit the demo's source code or configuration, faultctl, or the scoring harness. The agent may
   act ONLY through the Action API.
2. NEVER read feature-flag settings to diagnose an incident: no flagd files, no flag UI, no flag API.
   Diagnosis must come from telemetry.
   - Forbidden URLs and paths: http://localhost:8080/feature* , http://localhost:8080/flagservice/* ,
     flagd ports 8013 and 8016, the demo's src/flagd/ folder, demo.flagd.json.
3. NEVER run any action without human approval (read-only dry runs are allowed).
4. NEVER hard-code answers to the practice incidents. The final evaluation uses UNSEEN incidents,
   including infrastructure faults and combinations.
5. NEVER touch anything outside our own laptop, anything that affects other teams, or the venue network.
6. NEVER attack real or production systems.

Also: everything runs on our own laptop, and nothing is deployed to shared infrastructure. Any language,
agent framework or open-source library is allowed. The agent's output for each incident MUST follow the
provided JSON schema so it can be scored.

## 4. Starter kit (provided)
| Item | What it is | Source |
|---|---|---|
| OpenTelemetry Demo | Microservices store (frontend, cart, checkout, payment, product catalog, recommendation, ad, shipping and more) with a load generator; runs with Docker Compose | Open source, GitHub |
| Telemetry | Traces, metrics and logs from every service through the OpenTelemetry Collector, with bundled trace and metrics viewers | Comes with the demo |
| Failure flags | Built-in switches for realistic failures, managed by flagd (OFF-LIMITS for diagnosis) | Comes with the demo |
| faultctl | CLI to inject a practice incident (a flag or an infrastructure fault) and reset everything | Organisers |
| Action API | The ONLY allowed way for the agent to change the system: restart a service, toggle a feature flag, throttle the load generator, scale a service (Kubernetes only). Every call is logged | Organisers |
| Offline incident bundles | Recorded telemetry for every practice incident, for laptops that cannot run the full demo | Organisers |
| LLM access | API key with a spend limit; open-source or local models also allowed | Organisers |
| Template repository | Agent skeleton, required output format (JSON schema), README template, team form | Organisers |

Ten practice incidents are published at kickoff.

## 5. Incident types the agent may face
Application faults (the demo's built-in failure flags):
- The payment service fails when charging
- The cart service fails intermittently
- The product catalog fails for one specific product
- A memory leak in the recommendation service from a runaway cache
- High CPU or heavy garbage collection in the ad service
- A traffic flood on the homepage

Infrastructure faults (injected by organisers):
- A service container is stopped or keeps restarting
- The cache or a database becomes unavailable
- Network latency or packet loss between two services
- A service is starved of CPU or memory

Unseen incidents in evaluation can include combinations of the above.

## 6. Timeline and checkpoints
### Day 1 (11:30 to 18:45)
| Time | Milestone | Expected outcome |
|---|---|---|
| 11:30–12:00 | Kickoff | Problem reveal, starter kit walkthrough, rules |
| 12:00–13:00 | Setup | Demo running with load; faultctl injects and resets an incident; LLM key works; repo forked; all 3 members have committed |
| 13:00 | Checkpoint 0: Setup | Mentor confirms setup; blockers fixed on the spot |
| 13:00–15:30 | The agent can see | Tools for logs, metrics, traces and deploy history; agent summarises the symptoms of one practice incident; 1-page architecture sketch |
| 15:30 | Checkpoint 1: Eyes | Live run on one practice incident, plus the sketch |
| 15:30–18:30 | The agent can diagnose | Root cause with evidence and confidence; output in the JSON schema |
| 18:30 | Checkpoint 2: Brain | Correct diagnosis on at least 3 practice incidents chosen by the mentor |
| 18:45 | Day 1 wrap-up | Day 1 code state saved; mentors review progress |

Minimum by end of Day 1: an agent that uses tools and correctly diagnoses practice incidents with real evidence.

### Day 2 (09:00 to 18:45)
| Time | Milestone | Expected outcome |
|---|---|---|
| 09:00–09:30 | Day 2 alignment | Briefing on goals; review Day 1 feedback |
| 09:30–12:00 | The agent can act safely | Allowlisted action proposed; approval gate; execute; recovery verified; dry run and kill switch; incident report generated |
| 12:00 | Checkpoint 3: Hands | One full run, alert to verified recovery, with the action log |
| 12:00–13:30 | Harden, document, prep submission | No new features; fix bugs; clean run from scratch; finish README, evaluation note and team form |
| 13:30–14:00 | Final verification and submission | Final code push; verify schema compliance and deliverables |
| **14:00** | **CODE FREEZE, HARD CUTOFF** | Final commit recorded; later commits are ignored |
| 14:00–15:30 | Checkpoint 4: hidden test and mentor review | Organisers run unseen incidents on our setup; automated scoring and reviewer evaluations |
| 15:30–16:30 | Evaluation and elimination round | Mentors and judges review scores and select finalists |
| 16:30–18:45 | Finalist demos and individual interviews | 5-minute live demo plus 3 minutes of questions; short one-to-one technical conversations |

Minimum by code freeze: a full loop on at least one incident (diagnose, approve, act, verify, report),
a kill switch that works, and all deliverables in the repository.

### Prep requirements
- Docker with Compose v2, Python 3.11.
- The demo needs about 6 GB RAM and 14 GB disk (16 GB laptop is comfortable). On an 8 GB laptop use
  minimal mode (about 3 GB, without Kafka and the flag UI) or the offline bundles.
- Demo images were on USB drives at the venue.

## 7. Demo environment facts (verified from the opentelemetry-demo repo, main = v3.1.0)
Verified against our local checkout `C:\Placement\opentelemetry-demo` (commit ad06a6f9): the load
generator is **Locust** (`LOCUST_*` in .env, autostart, 5 users), not k6. Images are tagged
`${IMAGE_NAME}:${DEMO_VERSION}-<service>` with `DEMO_VERSION=latest` in .env (IMAGE_VERSION=3.1.0);
if the USB images carry a different tag, prefix compose with `DEMO_VERSION=<tag>` (never edit .env).
Extra service in this version: `telemetry-docs`.

### Expected telemetry (read from src/otel-collector/otelcol-config*.yml and src/prometheus/prometheus-config.yaml; confirm live)
- Metrics reach Prometheus by OTLP push (`--web.enable-otlp-receiver`), not scraping. Resource
  attributes `service.name`, `service.criticality`, `container.name` and others are promoted to labels
  (`service_name`, `service_criticality`, `container_name`).
- **Interval is about 60 s**: no `OTEL_METRIC_EXPORT_INTERVAL` is set (SDK default 60 s) and Prometheus
  comments that it aligns with it. Use rate windows of at least 3m (team: 1m returns empty); alerts lag 2–3 min.
  Traces, logs and docker state are near real time.
- **Span metrics** (`span_metrics` connector, defaults): expected `traces_span_metrics_calls_total` and
  `traces_span_metrics_duration_milliseconds_bucket`, labels `service_name`, `span_name`, `span_kind`,
  `status_code`. Span names are rewritten by `set_semconv_span_name`; the original is kept in the
  `original_span_name` attribute.
- **Container stats** (`docker_stats` receiver via the mounted docker socket): `container.cpu.*`,
  `container.memory.*` including usage/limit/percent, rss and hierarchical limit, `container.uptime`,
  network packets and block I/O, labelled by `container_name`.
- **Valkey** (`redis` receiver on valkey-cart:6379): `redis.*` (clients, memory, commands, uptime).
  Gaps in these series are themselves a signal that the cache is down.
- **PostgreSQL** (`postgresql` receiver, in the BASE config, not only full): `postgresql.*` (backends,
  commits, rollbacks, deadlocks, tuples, blocks) plus query-sample and top-query log events.
- **Host** (`host_metrics` on /hostfs = the Docker VM on Windows): `system.cpu.utilization`,
  `system.memory.utilization`, load, disk, network, paging, processes.
- **Availability probe** (`http_check/frontend-proxy`): `httpcheck.status` / `httpcheck.duration`.
- **nginx** (image-provider status) and **ad** (Prometheus scrape of ad:9465 for custom ad metrics).
- **Runtime/GC metrics** come from each SDK over OTLP (JVM for ad; .NET for cart; Node for payment
  and frontend; Go; Python). Exact names vary, so discover them with `list_metrics`.
- Exact Prometheus names depend on Prometheus 3.13's OTLP name translation (unit and type suffixes,
  e.g. `_bytes`, `_ratio`, `_total`). Discover them via `/api/v1/label/__name__/values`.
- **Traces** go to Jaeger 2.21 (`otlp_grpc/jaeger`). **Logs** go to OpenSearch index
  `otel-logs-yyyy-MM-dd` (only services that log over OTLP). `docker logs` remains the baseline.

### Compose layers
- `compose.yaml`: core shop only. In this layer the collector exports ONLY to its debug output, so
  there is NO Jaeger, Prometheus or OpenSearch.
- `compose.observability.yaml`: adds Jaeger, Prometheus, OpenSearch, Grafana and an OpAMP server.
- `compose.full.yaml`: adds Kafka, accounting and fraud-detection.
- Default full stack: `docker compose -f compose.yaml -f compose.full.yaml -f compose.observability.yaml up`
- If a laptop runs without the observability layer, our metrics and traces tools have nothing to
  query; use the offline bundles there.

### Endpoints reachable from the laptop
| What | Address |
|---|---|
| Shop through Envoy proxy | http://localhost:8080 |
| Prometheus HTTP API | http://localhost:9090 (fixed host port mapping) |
| Jaeger | through proxy at http://localhost:8080/jaeger/ (discover the API path, e.g. /jaeger/ui/api/services), or `docker compose port jaeger 16686` |
| Grafana | http://localhost:8080/grafana/ |
| OpenSearch (logs) | random host port: `docker compose port opensearch 9200` |
| Envoy admin stats | http://localhost:10000 |
| Load generator | controlled through the Action API only |
| FORBIDDEN | /feature, /flagservice/, flagd 8013/8016 |

All other service ports are container-internal only (published to random host ports).

### Logs
- Every container uses the json-file log driver (max 5 MB × 2 files), so `docker logs --since <t> <name>`
  works for all services. container_name equals the service name.
- Logs also go to OpenSearch through the collector when the observability layer runs.
- Some services (e.g. mcp) only log to stdout; some log over OTLP. docker logs is the reliable baseline.

### Services: language, internal port, memory limit, criticality
| Service | Lang | Port | Mem limit | Criticality |
|---|---|---|---|---|
| frontend-proxy (Envoy) | C++ | 8080 | 90M | critical |
| frontend | TypeScript/Node | 8080 | 250M | critical |
| checkout | Go | 5050 | 20M | critical |
| payment | Node.js | 50051 | 140M | critical |
| cart | .NET | 7070 | 160M | high |
| currency | C++ | 7001 | 20M | high |
| product-catalog | Go | 3550 | 20M | high |
| shipping | Rust | 50050 | 20M | high |
| ad | Java | 9555 (prom 9465) | 300M | medium |
| email | Ruby | 6060 | 100M | medium |
| recommendation | Python | 9001 | 500M | medium |
| quote | PHP | 8090 | 40M | low |
| image-provider | nginx | 8081 | 120M | low |
| load-generator | Locust (Python) | 8089 web | 512M | low |
| valkey-cart (cache) | | 6379 | 20M | |
| astronomy-db (Postgres) | | 5432 | 80M | |
| kafka (full layer) | | 9092 | | |
| otel-collector | | 4317/4318 | 400M | |
| jaeger | | 16686 | 1200M | |
| prometheus | | 9090 | 200M | |
| opensearch | | 9200 | 1G | |
| grafana | | 3000 | 175M | |

- All app services have `restart: unless-stopped`; most have Docker healthchecks.
- Several services are capped at 20 MB, so OOM kills and restart loops are a realistic failure mode.
  Container health MUST report State.Status, State.OOMKilled, RestartCount, Health.Status, StartedAt,
  and CPU and memory usage vs limit.
- Resource attribute `service.criticality` (critical/high/medium/low) is set on every service; use it
  to prioritise.

### Service dependency map (from compose depends_on and service addresses)
```
load-generator → frontend-proxy → frontend
frontend → ad, cart, checkout, currency, product-catalog, quote, recommendation, shipping, image-provider
checkout → cart, currency, email, payment, product-catalog, shipping, (full) kafka
cart → valkey-cart
product-catalog → astronomy-db (Postgres)
recommendation → product-catalog
shipping → quote
kafka → accounting, fraud-detection (full layer)
all services → otel-collector → jaeger, prometheus, opensearch
```
The agent uses this to walk from the alerting service (often frontend or checkout) to the ORIGIN.

### Metrics
- Span metrics (calls, errors, duration per service) are produced from traces. Exact metric and label
  names vary by version: discover them via GET /api/v1/label/__name__/values before writing queries.
  Typical: traces_span_metrics_calls_total{service_name, status_code="STATUS_CODE_ERROR"} and
  traces_span_metrics_duration_milliseconds_bucket.
- Runtime metrics per language (JVM GC and memory for ad, process/container CPU and memory), and
  container metrics if the collector's docker receiver is enabled. Discover; do not assume.

## 8. Diagnostic signatures (reasoning method, NOT answers; never hard-code)
| Fault class | What telemetry shows |
|---|---|
| A service fails on its calls | Error spans ORIGINATE in that service (deepest failing span); callers show propagated errors |
| A service is unreachable | Callers' client spans fail with connection errors; the target has NO matching server spans |
| Fails for one input (e.g. one product) | Errors cluster on one span attribute value |
| Memory leak | Memory climbs steadily toward the limit, latency rises, then possibly OOM kill and restart |
| High CPU / GC pressure | CPU near limit, GC metrics spike, latency up, few errors |
| Traffic flood | Request rate jumps at frontend/proxy; latency rises across many services |
| Container stopped / restarting | docker state not running, RestartCount climbing, OOMKilled; its spans vanish |
| Cache / DB down | valkey-cart or astronomy-db unhealthy; cart or product-catalog show connection errors |
| Network latency/loss between A and B | A's CLIENT span to B is much longer than B's SERVER span for the same call; timeouts/retries |
| CPU/memory starvation | Usage pinned at limit, throttling, latency up, possibly OOM |
| Combination | More than one origin; report each with its own evidence, or lower confidence and escalate |

Rules of reasoning:
- The loudest service is often a victim. Follow the dependency map and trace call chains to the deepest failure.
- Compare against a healthy baseline (captured before injecting faults) and quote before/after numbers.
- No telemetry from a service is itself evidence (stopped container or broken network), not a gap.
- If evidence is weak or conflicting, lower confidence and escalate with findings.

## 9. Architecture plan
```
watcher (thresholds, no LLM) ──alert──▶ agent loop (LLM + tools, max ~12 steps)
                                            │
         ┌──────────────┬──────────────┬────┴──────────┬──────────────┬───────────────┐
   service_health   get_metric   find_error_traces  search_logs  container_health  deploy_history
   (Prometheus)    (Prometheus)     (Jaeger)        (docker logs/   (docker inspect/  (organisers +
                                                    OpenSearch)     stats)           container starts
                                                                                     + Action API log)
                                            │
                              evidence registry (every result gets an ID, e.g. E7)
                                            │
                     diagnosis (JSON schema; cites only registry IDs; confidence 0–1)
                                            │
                   propose fix (allowlisted Action API action + reason) ── or ── escalate
                                            │
                   human approval gate (dry-run default; explicit "yes"; kill switch)
                                            │
                   execute via Action API → verify recovery (metrics vs baseline) → rollback + escalate if not
                                            │
                   incident report (timeline, root cause, evidence, action, prevention)
```

### Design rules
- Every tool returns a SMALL summarised dict (aggregated, deduplicated, capped ~2 KB), never raw dumps.
- Every tool registers its outputs in evidence/registry.py and returns evidence IDs.
- The diagnosis may cite only IDs present in the registry; a validator drops any claim whose ID is missing.
- Tools support source="live" | "bundle" (offline incident bundles), so every teammate can develop.
- Each tool has a CLI entry point for standalone testing.
- The agent prints every tool call and a one-line result live (needed for demos).
- Every run is saved to runs/<timestamp>.json.
- Python 3.11, simple readable code, no unnecessary frameworks.

### Planned tools
1. get_service_health(window): error rate, request rate, p95 latency per service, worst first; optional baseline comparison (`--save-baseline`).
2. get_metric(promql, window): min/max/latest/trend summary for ad-hoc queries.
3. find_error_traces(service, lookback, limit): trace_id, failing span service/operation, status message, duration, call chain; identify the DEEPEST failing span; client vs server duration per hop.
4. search_logs(service, since, level, contains): normalised and grouped patterns with count, first/last seen, one example; top 15.
5. get_container_health(): status, OOMKilled, restart count, health, uptime, CPU%, memory vs limit for every container.
6. get_deploy_history(): organisers' source if provided, container start/restart times, Action API call log.

### Day 2 additions
- Action API client: allowlisted actions only; dry-run default; every call logged.
- Approval gate: shows diagnosis + plan + expected effect; waits for an explicit "yes".
- Kill switch: one command/file/flag that stops the agent immediately and blocks further actions.
- Verify: re-check service health vs baseline after an action, with a timeout; roll back and escalate on failure.
- Report generator: timeline, root cause, evidence IDs, action taken, prevention.

## 10. Deliverables (must be in the repo by 14:00 Day 2)
- Agent code; output per incident in the provided JSON schema.
- README (template provided).
- Evaluation note.
- Team form.
- 1-page architecture sketch (ARCHITECTURE.md with a mermaid diagram).
- Action log, run outputs.

## 11. Team split
- Person A: get_service_health, get_metric, find_error_traces.
- Person B: search_logs, get_container_health, get_deploy_history, offline bundle adapter.
- Person C: config, evidence registry, agent loop, prompt, output schema, ARCHITECTURE.md.
Commit after every working piece; checkpoints judge what is in the repo.

## 12. Open questions for organisers/mentors
- Exact alert format, and does the harness deliver alerts for unseen incidents?
- Action API endpoints, auth and the full list of allowlisted actions.
- Where deployment history comes from.
- Offline bundle format.
- Which compose files "minimal mode" uses.
- Exact JSON schema fields (read from the template repo).
