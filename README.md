# Incident Zero: AI On-Call Engineer

See [CLAUDE.md](CLAUDE.md) for the problem, the hard rules and the plan, and
[ARCHITECTURE.md](ARCHITECTURE.md) for the design.

## LLM providers

The agent loop is provider-independent ([oncall/agent/providers.py](oncall/agent/providers.py)).
The guard, sanitiser, step cap, kill switch, evidence validator and incident record are the same
for every provider, and the record's `metadata.provider` / `metadata.model` show what was used.

| `ONCALL_PROVIDER` | Model setting | Key (in `.env` only) | Notes |
|---|---|---|---|
| `anthropic` (default) | `ONCALL_MODEL` (default `claude-opus-5-5`) | `ANTHROPIC_API_KEY` | Adaptive thinking, effort, refusal fallback; cost estimated in the record |
| `gemini` | `GEMINI_MODEL` (default `gemini-3.5-flash-lite`) | `GEMINI_API_KEY` | Gemini's OpenAI-compatible endpoint; needs `pip install openai`; cost is null in the record |

The provider is chosen only from `ONCALL_PROVIDER`, never from which keys happen to be set. Any
provider failure becomes the run outcome `llm_error`. The Gemini provider is **based on Shivakumar's
Day 1 Gemini adapter** (his reply-to-blocks conversion), rebuilt to translate our single
conversation on every call instead of keeping a second history. Mentors confirmed any model or API
key may be used.

The system prompt also includes three reasoning rules **based on Padma's checkpoint3 branch**:
- keep service identities exact (a stopped container is evidence about that service, not its callers)
- never claim OOM from an exit code alone (137 can be a SIGKILL from `docker stop`, 143 is SIGTERM)
- query the alert's time window rather than arbitrary long windows

## Approval prompt

Each plan step is approved separately. The approval box shows the diagnosis, root cause, action,
reason, expected outcome, **risk** (the step's `risk_assessment`, or "not assessed") and the
rollback. The risk shown is saved in the step's approval record and in the report. Showing the risk
is **based on Shivakumar's Day 1 approval gate**.

The terminal bell rings just before each approval box, so someone at the laptop notices that a
human is needed. `--execute` combined with `--auto-approve`/`--yes` is refused at startup
("--execute requires a human approval; --auto-approve only works with dry runs."), in both
`python -m oncall.agent.run` and `scripts/demo.py`. Both are **based on Aniruddha's aj_ck_3
branch**.

## Reviewer agent (stretch goal 2, off by default)

With `ONCALL_REVIEWER=on`, one extra model call reviews the validated diagnosis before anything is
shown for approval ([oncall/agent/reviewer.py](oncall/agent/reviewer.py)). It uses the same
provider, with no investigation tools. It gets the alert, the diagnosis and the text of every
registered evidence item, and acts as a skeptical senior SRE trying to disprove the diagnosis:
victim versus origin, contradicting evidence, alternatives not ruled out, and claims their cited
evidence does not support.

**It can** return `agree`, `doubt` or `disagree` with objections, suggest an alternative root cause,
and lower confidence by up to 0.3. If confidence then falls below 0.6, the affected steps are
removed and escalated to a human, with the objections attached. The approval box shows a
`Reviewer` line, and the report and incident JSON have a Review section (verdict, objections,
confidence before and after, tokens, duration). Its tokens count towards the run's cost.

**It cannot** (enforced in code): raise confidence (the adjustment is clamped to [-0.3, 0]); add,
change or retarget a step; approve anything; or act on its suggested alternative, which is only
shown to the human. Objections citing evidence IDs that are not in the registry are dropped. The
kill switch is checked before the call. On any error or a timeout (`ONCALL_REVIEWER_TIMEOUT_S`,
default 30 s), the run continues exactly as without a reviewer and records "review skipped:
<reason>".

## Action API (our own)

> **Confirmed by mentor `<name>` at `<time>`:** teams build their own Action API. Ours is a
> separate local service. The agent stays read-only and changes the system only by calling this
> API over HTTP.

The agent (`oncall/`) never imports this package. It never runs a state-changing docker command,
and a test enforces that. Every change to the demo goes through this service, after a human
approves each step.

### Start it

```powershell
# in .env (gitignored):  ACTION_API_TOKEN=<a long random string>
#                        ACTION_API_URL=http://127.0.0.1:8099
python -m actionapi               # listens on 127.0.0.1:8099 (ACTION_API_PORT), localhost only
curl http://127.0.0.1:8099/health

# the agent then executes approved steps through it:
python scripts/demo.py stop-payment --wait 180 --execute
```

The service will not start without `ACTION_API_TOKEN`, and it refuses to bind anything but a
loopback address.

### Endpoints

| Method and path | Body | What it does |
|---|---|---|
| `GET /health` | | Liveness check (the only call that needs no token) |
| `POST /actions/restart_service` | `{"service": "payment"}` | `docker restart` (which also starts a stopped container) of an allowlisted application container. Returns the container state before and after |
| `POST /actions/throttle_load_generator` | `{"users": 2}` | Sets the Locust user count through Locust's own web API (`LOCUST_URL`, default `http://localhost:8080/loadgen`). Remembers the previous count |
| `POST /actions/unthrottle_load_generator` | `{}` | The inverse: restores the remembered user count. This is the agent's rollback for a throttle |
| `POST /actions/toggle_feature_flag` | | **501** "not implemented: pending mentor approval" |
| `POST /actions/scale_service` | | **501** "not supported on Docker Compose" |

Every action call:
- needs `Authorization: Bearer <ACTION_API_TOKEN>`
- may send `Idempotency-Key: <key>`. A retry with the same key returns the first result (`"replayed": true`) without acting again. The same key with a different body gets 409. The agent sends `<run_id>:step<N>:<action>`.
- may send `"dry_run": true`. The request is fully validated (allowlist, cooldown, container exists) and nothing is changed.
- gets back only success/failure and container or user-count state. Responses never include flag values, tokens or other secrets.

Status codes: 200 OK, 400 bad payload, 401 missing or wrong token, 403 refused, 404/405 unknown
route, 409 idempotency conflict or nothing to restore, 423 kill switch is on, 429 cooldown (with
`retry_after_s`), 501 not implemented, 502 docker or Locust failed.

### What it refuses, and why

| Refused | Why |
|---|---|
| Restarting anything not in `RESTART_ALLOWLIST` ([actionapi/config.py](actionapi/config.py)): only the shop's application services plus `valkey-cart` and `astronomy-db` | Only what a runbook may restart |
| Restarting `prometheus`, `jaeger`, `opensearch`, `grafana`, `otel-collector`, `opamp-server` | They are our eyes. Restarting them would hide the incident and break verification |
| Restarting `flagd`, `flagd-ui`, or any name containing `flagd` or `feature` | HARD RULE 2: feature-flag state is off-limits |
| Restarting `load-generator` | Traffic is controlled by throttling, not by killing the generator |
| Anything named like `faultctl` | Our fault injector is test harness only |
| Containers outside the `opentelemetry-demo` compose project | Never touch anything else on this laptop |
| A second change to the same service within `ACTION_API_COOLDOWN_S` (default 60 s) | Stops restart loops and flapping. The unthrottle (rollback) is exempt, so a rollback is never blocked |
| Every action request (dry runs and idempotent replays included; not `GET /health`) while the kill switch is on: **423** "kill switch is on" | The API enforces the kill switch itself, using the agent's `STOP` file (`ONCALL_KILL_SWITCH`, default `<repo>/STOP`; set by `python -m oncall stop`). The agent records a 423 as a refusal, never retries it, and logs it. **Based on Padma's checkpoint3 branch** |
| `toggle_feature_flag` | Until mentors confirm how flag names may be known; this API never reads or writes flag files or the flag API |
| `scale_service` | Kubernetes only; the demo runs on Docker Compose |

The service never edits any demo file. Locust is changed only through Locust's own HTTP
control API.

**How Locust is reached (verified 24 Oct 11:34 on the running demo).** The demo's Envoy proxy routes
`/loadgen/` to the load generator's web port and rewrites the prefix to `/` (see
`src/frontend-proxy/envoy.tmpl.yaml` in the demo, read only). Compose publishes Locust's own port
(`LOCUST_WEB_PORT`, 8089) only to a random host port, so `127.0.0.1:8089` is not reachable.
`GET http://localhost:8080/loadgen/stats/requests` returned 200 with `user_count` 5 and state
`running`, so the default `LOCUST_URL` is `http://localhost:8080/loadgen`.

### State across restarts

After every applied action the service saves its state to `runs/action_api_state.json`
(`ACTION_API_STATE`): the user count to restore after a throttle, the cooldowns and the idempotency
results. It writes a temp file, then renames it. An unthrottle (rollback) therefore still works
after the API is restarted, and a retried request is still not applied twice. An unreadable file is
ignored, so the API starts clean rather than guessing. **Based on Padma's checkpoint3 branch.**

### Audit log

Every request is appended to `runs/action_api.jsonl` (`ACTION_API_LOG`), whether it was
accepted, refused, a dry run or a failure. Each entry has the timestamp, caller (`X-Caller` or
client address), method and path, action, params (service or users only), dry-run flag,
idempotency key, HTTP status, result, replay flag and duration. This is the API's own record,
separate from the agent's `runs/actions.jsonl`.
