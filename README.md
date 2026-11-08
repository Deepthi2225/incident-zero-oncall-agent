# Incident Zero: AI On-Call Engineer

An AI agent that responds to a production incident in a microservices shop. It investigates using
real telemetry, names the root cause with evidence, proposes a safe fix, waits for a human to
approve it, applies it, and checks that the system recovered. When it is not sure, it hands over to
a human with what it found.

Built for the Bosch AI hackathon at PES University (23 and 24 October 2026), on the theme of agentic
AI for IT operations. It runs against the open-source
[OpenTelemetry Demo](https://github.com/open-telemetry/opentelemetry-demo).

See [ARCHITECTURE.md](ARCHITECTURE.md) for the design and [EVALUATION.md](EVALUATION.md) for results.

## What you need

| | Offline try-out | Full live demo |
|---|---|---|
| [Python](https://www.python.org/downloads/) 3.11 or newer, and Git | Yes | Yes |
| An LLM API key (see step 3) | Yes | Yes |
| [Docker](https://www.docker.com/products/docker-desktop/) with Compose v2 | No | Yes |
| Free memory and disk | Very little | About 6 GB RAM (16 GB laptop recommended), 14 GB disk |

It runs on Windows, macOS and Linux. Commands below are the same on all three unless noted.

## Quick start (offline, about 5 minutes)

This runs the agent on recorded data, so it needs no Docker and changes nothing.

**1. Get the code**

```
git clone https://github.com/aniruddhajoshi100/ocAgent.git
cd ocAgent
```

**2. Install it in a virtual environment**

Windows (PowerShell):
```
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

macOS or Linux:
```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

If PowerShell blocks the activation script, run
`Set-ExecutionPolicy -Scope Process RemoteSigned` and try again. With the environment active, `python`
works on every system. Activate it again in each new terminal.

**3. Add an LLM key**

Copy the settings file: `copy .env.example .env` on Windows, `cp .env.example .env` on macOS or Linux.
Then open `.env` and set one provider. [Groq](https://console.groq.com/keys) is the easiest way to start:

```
ONCALL_PROVIDER=groq
GROQ_API_KEY=your-key-here
```

`.env` is ignored by Git. Never commit it. Check the key with `python scripts/llm_check.py`.
It should print a line starting with `OK`.

**4. Run the tests** (optional; these need no key, Docker or network)

```
python -m pytest -q
```

**5. Run the agent on a recorded incident**

```
python -m oncall.agent.run tests/fixtures/payment_down/alert.json --test
```

You will see each tool the agent calls, then its diagnosis and proposed fix. Nothing is really
changed in this mode. The report is saved in `runs/`.

## Full live demo

**1. Start the shop.** Clone it next to this project and keep the folder name
`opentelemetry-demo`, because the agent uses it to recognise the shop's containers. This project was
tested on commit `ad06a6f9`.

```
git clone https://github.com/open-telemetry/opentelemetry-demo.git
cd opentelemetry-demo
git checkout ad06a6f9
docker compose -f compose.yaml -f compose.full.yaml -f compose.observability.yaml up -d
cd ../ocAgent
```

Do not edit anything inside that folder. Wait about 5 minutes for traffic to build up, then open
http://localhost:8080 to see the shop. If you did use another folder name, add
`COMPOSE_PROJECT=<that folder name>` to `.env`.

**2. Finish `.env`.** Add these two lines. Generate the token with
`python -c "import secrets; print(secrets.token_urlsafe(32))"`.

```
ACTION_API_TOKEN=paste-the-generated-token
ACTION_API_URL=http://127.0.0.1:8099
```

**3. Record what healthy looks like**, while the shop is running normally:

```
python -m oncall check
python -m oncall baseline --window 5m
python detector.py --calibrate 300
```

**4. Start the services**, each in its own terminal with the virtual environment active:

```
python -m actionapi
python -m oncall.ui
```

The first is the only component allowed to change the shop. The second is a read-only dashboard at
http://127.0.0.1:8090.

**5. Break something and watch the agent respond.** In a third terminal:

```
python scripts/demo.py stop-payment --wait 180 --execute
```

This stops the payment service, waits for the alert, and starts the agent. When it asks, answer `y`
to approve the fix or `n` to refuse. Without `--execute` it is a dry run.

**6. Put everything back:**

```
python -m faultctl reset
```

To run it continuously instead, use `python -m oncall watch`. It starts an investigation whenever
an alert fires. To see every fault you can practise on, use `python -m faultctl list`.

## Stop the agent immediately

```
python -m oncall stop
python -m oncall resume
```

`stop` blocks all further model calls, tool calls and actions, even ones already approved. `resume`
turns the agent back on.

## Common commands

| Command | What it does |
|---|---|
| `python scripts/llm_check.py` | Checks the LLM key and tool calling |
| `python -m oncall check` | Checks Prometheus, Jaeger and Docker can be reached |
| `python -m oncall baseline --window 5m` | Saves healthy numbers to compare against |
| `python -m oncall watch` | Watches for alerts and investigates them |
| `python -m oncall investigate --service checkout --signal error_rate --value 0.3` | Investigates a manual alert |
| `python -m oncall verify <run_id>` | Checks recovery after a human fixed something by hand |
| `python -m oncall emailtest` | Sends one test email |
| `python -m faultctl list` / `inject <id>` / `status` / `reset` | Practice faults |

## Troubleshooting

| Problem | Fix |
|---|---|
| `python` is not found (macOS or Linux) | Use `python3`, or activate the virtual environment |
| `No module named ...` | Activate the virtual environment and run `pip install -r requirements.txt` |
| Run ends with `llm_error` | Run `python scripts/llm_check.py`, then check the key and `ONCALL_PROVIDER` in `.env` |
| Groq `Request too large` or repeated rate limits | Turn on the Ollama fallback below, or use a paid tier |
| `python -m oncall check` fails | Make sure the shop is up and has run for a few minutes |
| No metrics or an empty report | Metrics arrive about once a minute. Wait 2 to 3 minutes |
| Actions are refused as outside the project | Set `COMPOSE_PROJECT` to the shop's folder name in `.env` |
| Agent says the kill switch is on | Run `python -m oncall resume` |
| Docker runs out of memory | Give Docker at least 6 GB in its settings |

## Settings

All settings go in `.env`. [.env.example](.env.example) lists them.

### LLM providers

| `ONCALL_PROVIDER` | Default model (setting) | Key |
|---|---|---|
| `anthropic` (default) | `claude-opus-5-5` (`ONCALL_MODEL`) | `ANTHROPIC_API_KEY` |
| `groq` | `openai/gpt-oss-120b` (`GROQ_MODEL`) | `GROQ_API_KEY` |
| `gemini` | `gemini-3.5-flash-lite` (`GEMINI_MODEL`) | `GEMINI_API_KEY` |
| `ollama` | `qwen3:8b` (`OLLAMA_MODEL`) | None; runs on your machine |

The provider comes only from `ONCALL_PROVIDER`, never from which keys are set. If a provider fails,
the run ends as `llm_error` and escalates to a human. On Groq, rejected tool calls are retried and
per-minute rate limits are waited out.

### Local fallback with Ollama (optional)

If the main provider fails, a local model can answer instead. It needs a GPU with about 8 GB of
memory to run at a usable speed.

1. Install [Ollama](https://ollama.com) and run `ollama pull qwen3:8b`.
2. Set `OLLAMA_CONTEXT_LENGTH=16384` as an environment variable and restart Ollama. A smaller value
   silently cuts off the conversation.
3. Add `ONCALL_FALLBACK_PROVIDER=ollama` to `.env`.

The record shows when the fallback answered. **A diagnosis the fallback helped produce never offers
a fix**: the findings go to a human, because the smaller model is less reliable.

### Reviewer (optional)

`ONCALL_REVIEWER=on` adds one skeptical second opinion before anything is shown for approval. It can
lower confidence and remove steps, but it can never raise confidence, add a step or approve anything.

### Email alerts (optional, off by default)

The agent emails a human when it escalates or needs approval. Each email has a formatted version, a
plain-text copy and, once the run has ended, the report attached. Runs on recorded data are marked
`[TEST]`. Email sends incident data off your machine.

With [Resend](https://resend.com):
```
EMAIL_ENABLED=on
EMAIL_TRANSPORT=resend
RESEND_API_KEY=your-key-here
EMAIL_TO=you@example.com
```

Without a verified domain, Resend only delivers to its account owner's own address. Set `EMAIL_FROM`
once you have verified one.

With SMTP:
```
EMAIL_ENABLED=on
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=your-user
SMTP_PASSWORD=your-password
SMTP_STARTTLS=on
EMAIL_FROM=oncall-agent@example.com
EMAIL_TO=you@example.com
```

Check it with `python -m oncall emailtest`. You cannot approve a fix by replying to an email.

## How it stays safe

- **The agent only reads.** Every change goes through the Action API, a separate local service that
  needs a token and listens only on your own machine.
- **Allowed actions only.** It can restart a shop service or throttle the load generator. Feature-flag
  toggling and scaling return "not implemented", so those faults go to a human.
- **It leaves its own monitoring alone.** Prometheus, Jaeger, the collector and other monitoring
  containers cannot be restarted, and nothing outside the shop's Compose project can be touched.
- **A human approves every step.** `--execute` cannot be combined with `--auto-approve`.
- **Evidence is checked.** Any claim citing evidence the agent did not actually collect is dropped.
- **Recovery is verified.** After a fix, the agent checks health against the baseline, then rolls back
  and escalates if the system did not recover.
- **No feature-flag data.** Flag settings are never read, and flag data is stripped from all telemetry.
- **Everything is logged.** Results are in `runs/`, including each run's record and report, and the
  Action API's audit log in `runs/action_api.jsonl`.

## Action API reference

Runs at `http://127.0.0.1:8099`. Every call except `GET /health` needs
`Authorization: Bearer <ACTION_API_TOKEN>`.

| Method and path | Body | What it does |
|---|---|---|
| `GET /health` | | Liveness check |
| `POST /actions/restart_service` | `{"service": "payment"}` | Restarts an allowed shop service |
| `POST /actions/throttle_load_generator` | `{"users": 2}` | Lowers the simulated user count |
| `POST /actions/unthrottle_load_generator` | `{}` | Restores the previous user count |
| `POST /actions/toggle_feature_flag` | | 501, not implemented |
| `POST /actions/scale_service` | | 501, needs Kubernetes |

Add `"dry_run": true` to validate a request without changing anything. An `Idempotency-Key` header
makes retries safe. A second change to the same service within 60 seconds is refused
(`ACTION_API_COOLDOWN_S`).

Status codes: 200 OK, 400 bad request, 401 bad token, 403 refused, 404/405 unknown route,
409 conflict, 423 kill switch on, 429 cooldown, 501 not implemented, 502 Docker or load generator failed.

## Contributors

- Deepthi ([Deepthi-PES1UG23AM092](https://github.com/Deepthi-PES1UG23AM092))
- Aniruddha Joshi ([aniruddhajoshi100](https://github.com/aniruddhajoshi100))
- Shivakumar ([ShivakumarSK2005](https://github.com/ShivakumarSK2005))
- Padma
- Nirupam

## License

[MIT](LICENSE)

## Contributing

Create a branch from `main`, keep `python -m pytest -q` passing, add tests for what you change, and
open a pull request. Never edit the shop's code, read its feature flags, or commit `.env` or `runs/`.
