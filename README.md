# P0ppy

**Your little buddy for big incidents.**

P0ppy is an AI on-call engineer. When a production service breaks, it investigates using real
telemetry, names the root cause with evidence, proposes a safe fix, waits for a person to approve it,
applies it, and checks that the system really recovered. When it isn't sure, it says so and hands
its findings to a person.

It runs against the open-source [OpenTelemetry Demo](https://github.com/open-telemetry/opentelemetry-demo),
a realistic microservices shop.

![The P0ppy dashboard during a sample incident with two root causes](docs/dashboard.png)

*The dashboard, shown here with sample data.*

## What it does

| | |
|---|---|
| **Detects** | Watches error rate, latency and silent services against a recorded healthy baseline |
| **Investigates** | An LLM chooses which traces, metrics, logs and container checks to run next |
| **Proves it** | Every claim must cite evidence P0ppy really collected; anything else is dropped |
| **Handles the messy cases** | Several faults at once, faults it has never seen, and new alerts during a run |
| **Suggests fixes** | Approved actions it can run, listed separately from its own advice, which it never runs |
| **Asks first** | Nothing changes without a person's approval, from the dashboard or the terminal |
| **Verifies** | Compares health against the baseline after a fix, and rolls back if it didn't work |
| **Reports** | A report and draft postmortem for every incident |
| **Alerts** | Email, phone call, Telegram or Slack, routed by severity |

## Try it in 5 minutes (no Docker)

This runs P0ppy on recorded incident data, so nothing is changed. You need
[Python 3.11+](https://www.python.org/downloads/), Git, and an LLM key.
[Groq](https://console.groq.com/keys) has a free tier.

```
git clone https://github.com/Deepthi2225/incident-zero-oncall-agent.git
cd incident-zero-oncall-agent
python scripts/bootstrap.py
```

On macOS or Linux, use `python3` for that last command. Then open `.env` and set your provider:

```
ONCALL_PROVIDER=groq
GROQ_API_KEY=your-key-here
```

Activate the environment. You need to do this in each new terminal, and after it `python` works on every system.

| System | Command |
|---|---|
| Windows (PowerShell) | `.venv\Scripts\Activate.ps1` |
| macOS or Linux | `source .venv/bin/activate` |

If PowerShell blocks the script, run `Set-ExecutionPolicy -Scope Process RemoteSigned` first.

Now run an incident:

```
python -m oncall.agent.run tests/fixtures/payment_down/alert.json --test
```

You'll see each tool it calls, then its diagnosis and proposed fix. The report is saved in `runs/`.

## Run it for real

This needs [Docker](https://www.docker.com/products/docker-desktop/) with Compose v2, about 6 GB of free
memory and 14 GB of disk. A 16 GB laptop is comfortable.

**1. Start the shop.** Clone it next to this project and keep the folder name, because P0ppy uses it to
recognise the shop's containers. Don't edit anything inside it. If you must use another folder name, add
`COMPOSE_PROJECT=<folder name>` to `.env`.

```
cd ..
git clone https://github.com/open-telemetry/opentelemetry-demo.git
cd opentelemetry-demo
git checkout ad06a6f9
docker compose -f compose.yaml -f compose.full.yaml -f compose.observability.yaml up -d
cd ../incident-zero-oncall-agent
```

Wait about 5 minutes for traffic to build up.

**2. Create your account.** The password is typed at a hidden prompt and needs 12 or more characters.

```
python -m oncall user add yourname --role admin
```

**3. Record what healthy looks like,** while the shop is running normally. This takes about 5 minutes.

```
python -m oncall baseline --window 5m
python detector.py --calibrate 300
```

**4. Check that everything is ready.** This tells you exactly what's missing and how to fix it.

```
python -m oncall doctor
```

**5. Turn on approvals from the dashboard** by adding `UI_APPROVALS=on` to `.env`.

**6. Start P0ppy:**

```
python -m oncall up --watch --execute
```

Open http://127.0.0.1:8090 and sign in. Keep this terminal open; **Ctrl+C** stops everything.
Without `--execute`, approved fixes are a dry run and nothing in the shop changes.

## Test it with a practice incident

With P0ppy running, break something from a second terminal (activate the environment first):

```
python -m faultctl inject stop-payment
```

1. Within about 3 minutes the alert fires. Metrics arrive once a minute, so this delay is normal.
2. P0ppy investigates, and the banner changes to **P0ppy needs your yes**.
3. Click **Review and approve**, check what it will do, and enter your password.
4. P0ppy restarts the service and verifies that it recovered.
5. Open **Investigate** to read the evidence and postmortem.

Then put everything back with `python -m faultctl reset`. `python -m faultctl list` shows every practice fault.

## Use it from your phone

[Tailscale](https://tailscale.com) gives your own devices a private, encrypted way in, without putting the
dashboard on the internet.

1. Install Tailscale on your computer and phone, and sign in to both with the same account.
2. In the [admin console](https://login.tailscale.com/admin/dns), turn on MagicDNS and HTTPS Certificates.
3. Run `tailscale serve --bg 8090`. It prints an address like `https://your-laptop.your-tailnet.ts.net`.
4. Add `UI_ALLOWED_HOSTS=your-laptop.your-tailnet.ts.net` to `.env`, then restart P0ppy.

Your computer has to be on, with P0ppy running. Use 2-step verification on your Tailscale sign-in account,
and never use `tailscale funnel`, which would make it public. To remove the link: `tailscale serve --https=443 off`.

## The dashboard

| Tab | What's there |
|---|---|
| **Overview** | The service map, what went wrong, how to fix it, team notes, shop health, who was told |
| **Investigate** | Start a run, see each step P0ppy took, the evidence, the postmortem |
| **History** | Search and filter past incidents |
| **Settings and logs** | Setup check, alert channels, your account, team accounts, logs, the audit log |

| Role | Can do |
|---|---|
| `viewer` | See everything |
| `responder` | Also take ownership, hand over, add notes, start dry-run investigations, verify a manual fix |
| `approver` | Also approve or reject fixes, start live runs, use the kill switch |
| `admin` | Also manage accounts and send test alerts |

## Common commands

| Command | What it does |
|---|---|
| `python -m oncall doctor` | Checks setup and says what to fix |
| `python -m oncall up [--watch] [--execute]` | Starts the Action API and dashboard, and optionally the watcher |
| `python -m oncall stop` / `resume` | Kill switch: blocks all model calls, tools and fixes |
| `python -m oncall investigate --service checkout --signal error_rate` | Investigates by hand (add `--execute` for real fixes) |
| `python -m oncall verify <run_id>` | Checks recovery after you fixed something yourself |
| `python -m oncall ack <incident>` | Acknowledges an incident; with no id, lists those waiting |
| `python -m oncall user add\|list\|remove` | Manages accounts |
| `python -m oncall whoison` | Shows who a rotation has on call today |
| `python -m oncall emailtest` / `calltest` / `telegramtest` / `slacktest` | Sends one labelled test alert |
| `python -m faultctl list` / `inject <id>` / `status` / `reset` | Practice faults |
| `python -m pytest -q` | Runs the tests; they need no key, Docker or network |

## Settings

All settings go in `.env`, which Git ignores. [.env.example](.env.example) lists them.

### LLM providers

| `ONCALL_PROVIDER` | Default model | Key |
|---|---|---|
| `anthropic` (default) | `claude-opus-5-5` | `ANTHROPIC_API_KEY` |
| `groq` | `openai/gpt-oss-120b` | `GROQ_API_KEY` |
| `gemini` | `gemini-3.5-flash-lite` | `GEMINI_API_KEY` |
| `ollama` | `qwen3:8b` | None; runs on your machine |

To use a local model if your main provider fails, install [Ollama](https://ollama.com), run `ollama pull qwen3:8b`,
set the environment variable `OLLAMA_CONTEXT_LENGTH=16384`, and add `ONCALL_FALLBACK_PROVIDER=ollama`. It needs about
8 GB of GPU memory. **A diagnosis the fallback helped produce never offers a fix**, because the smaller model is less reliable.

`ONCALL_REVIEWER=on` adds a skeptical second opinion, which can lower confidence or remove steps but never add them.

### Alerts

All alerts are off by default, and runs on recorded data never ring anyone.

| Channel | Add to `.env` | Test |
|---|---|---|
| Email (SMTP) | `EMAIL_ENABLED=on`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_STARTTLS=on`, `EMAIL_FROM`, `EMAIL_TO` | `emailtest` |
| Email (Resend) | `EMAIL_ENABLED=on`, `EMAIL_TRANSPORT=resend`, `RESEND_API_KEY`, `EMAIL_TO` | `emailtest` |
| Phone (SEV1, SEV2) | `CALLS_ENABLED=on`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `ONCALL_PRIMARY_PHONE` | `calltest` |
| Telegram | `TELEGRAM_ENABLED=on`, `TELEGRAM_BOT_TOKEN`, then message your bot and run `telegramtest` to get `TELEGRAM_CHAT_ID` | `telegramtest` |
| Slack | `SLACK_ENABLED=on`, `SLACK_WEBHOOK_URL` | `slacktest` |

Twilio trial accounts can only call verified numbers. The spoken summary passes through Twimlets, Twilio's free text-to-call page.

To alert different people by severity, copy `escalation.example.json` to `escalation.json` and fill it in. It also
supports on-call rotations and escalating when nobody acknowledges in time. Anything missing falls back to `.env`,
and test runs only ever reach the `.env` contacts.

## How it stays safe

- **The agent only reads.** Every change goes through the Action API, a separate local service that needs a token.
- **Allowed actions only.** It can restart a shop service or throttle the load generator. Feature-flag toggling and
  scaling aren't available, so those faults go to a person.
- **Its own ideas are only advice.** Suggestions never enter the plan and can never run.
- **A person approves every step,** and nobody answering means nothing happens. `--execute` can't be combined with `--auto-approve`.
- **It leaves its monitoring alone,** and nothing outside the shop's Compose project can be touched.
- **Evidence is checked,** and a label without the right kind of evidence has its confidence capped.
- **Recovery is verified,** with rollback and escalation if it failed.
- **No feature-flag data.** Flag settings are never read, and flag data is stripped from all telemetry.
- **The dashboard is locked down:** hashed passwords, roles, login throttling, protected sessions, your password again
  before approving a fix, and an audit log of every attempt. It listens only on your own machine.
- **Secrets stay out of Git.** `.env`, `escalation.json` and `ui_users.json` are ignored.

## Troubleshooting

Start with `python -m oncall doctor`.

| Problem | Fix |
|---|---|
| `python` is not found (macOS or Linux) | Use `python3`, or activate the environment |
| `No module named ...` | Activate the environment, or run `python scripts/bootstrap.py` |
| Stuck on the sign-in page saying no accounts exist | Run `python -m oncall user add yourname --role admin` |
| Run ends with `llm_error` | Run `python scripts/llm_check.py`, then check the key and `ONCALL_PROVIDER` |
| Groq rate limits | The free tier is limited per day; add the Ollama fallback or wait |
| No metrics or an empty report | Metrics arrive about once a minute, so wait 2 to 3 minutes |
| A fix says "dry run" | Start with `--execute` |
| Nothing to approve on the dashboard | Add `UI_APPROVALS=on` to `.env` and restart |
| "Port is already in use" from `up` | P0ppy is already running; close the other terminal |
| Actions refused as outside the project | Set `COMPOSE_PROJECT` to the shop's folder name |
| Agent says the kill switch is on | Run `python -m oncall resume` |
| Docker runs out of memory | Give Docker at least 6 GB in its settings |

## Learn more

- [ARCHITECTURE.md](ARCHITECTURE.md): the design
- [EVALUATION.md](EVALUATION.md): results
- `results/`: saved runs, including several faults at once and unseen faults

## Contributors

P0ppy began as Incident Zero, a team project at the Bosch AI hackathon at PES University in October 2026
([original repository](https://github.com/aniruddhajoshi100/ocAgent)), and is now continued by Deepthi.

- Deepthi ([Deepthi2225](https://github.com/Deepthi2225))
- Aniruddha Joshi ([aniruddhajoshi100](https://github.com/aniruddhajoshi100))
- Shivakumar ([ShivakumarSK2005](https://github.com/ShivakumarSK2005))
- Padma
- Nirupam

## License

[MIT](LICENSE)
