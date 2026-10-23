# Incident Zero: architecture

An AI on-call engineer for the OpenTelemetry demo. A threshold watcher raises an alert. An LLM agent
then investigates with read-only tools, names the origin and its cause, and backs every claim with
an evidence ID. Day 2 adds the approval gate and the Action API.

```mermaid
flowchart TD
    W["watcher.py<br/>thresholds, no LLM<br/>2 consecutive breaches"] -->|alert JSON| A
    M["manual / organiser alert"] -->|alert JSON| A
    A["agent/loop.py<br/>Claude + tool use<br/>max 12 steps, kill switch"]
    A --> T1[get_service_health<br/>Prometheus span metrics vs baseline]
    A --> T2[find_error_traces<br/>Jaeger: deepest failing span,<br/>client vs server per hop]
    A --> T3[search_logs<br/>docker logs, grouped patterns]
    A --> T4[get_container_health<br/>docker inspect + stats]
    A --> T5[get_deploy_history<br/>container starts, our actions]
    A --> T6[list_metrics / get_metric<br/>runtime CPU, memory, GC]
    T1 & T2 & T3 & T4 & T5 & T6 --> R[(evidence registry<br/>E1, E2, ...)]
    T1 & T2 & T3 & T4 & T5 & T6 -.-> S{{source: live / bundle replay<br/>guard blocks flag endpoints}}
    A -->|submit_diagnosis| V[validator<br/>drops uncited claims,<br/>escalates if confidence < 0.6]
    R --> V
    V --> O[runs/&lt;timestamp&gt;.json<br/>diagnosis + evidence + tool trail]
    V -.->|Day 2| G[approval gate: dry run, explicit yes] -.-> X[Action API] -.-> C[verify vs baseline<br/>rollback + escalate]
```

## Key decisions
- **Agent picks the next tool.** The model sees only small (≤2 KB) summaries. It starts broad
  (service and container health) and then narrows to traces, logs and metrics of the suspects.
- **No invented evidence.** Every tool registers what it observed and returns IDs. The diagnosis
  can cite only those IDs, and the validator removes any claim without one. A missing signal
  ("no server spans from payment") is registered as evidence too.
- **Origin, not the loudest victim.** Trace analysis returns the deepest failing span and the call
  chain. A failed client call with no server span means the callee is unreachable. A large
  client-minus-server time on an edge means network trouble.
- **Escalating is a valid answer.** Low confidence or weak evidence sets `status: escalate`, with
  findings and next checks for a human.
- **Hard rules in code.** `sources/guard.py` refuses any URL, path or command that touches flagd or
  the flag UI. All tools are read-only. A `STOP` file halts the agent (`python -m oncall stop`).
- **Offline development.** Any live run can be recorded (`--record DIR`) and replayed later
  (`--source bundle --bundle DIR`).

## Running
```
pip install -r requirements.txt
python -m oncall check                        # verify Prometheus, Jaeger, docker (read-only)
python -m oncall baseline --window 5m         # with the demo healthy, before any fault
python -m oncall investigate --service frontend --signal error_rate --value 0.2
python -m oncall watch                        # watcher -> investigation, end to end
python -m oncall.tools.traces checkout        # any tool standalone
python -m pytest -q
```
