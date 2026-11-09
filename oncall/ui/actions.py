"""What a signed-in person can do from the dashboard. Nothing here touches the shop directly.

A fix is only ever a decision handed to the waiting agent, which then goes through the same checks
as a terminal approval: the kill switch, the Action API and verification. Each function takes
validated input from the JSON body and returns (HTTP status, result dict).
"""
import re
import subprocess
import sys

from oncall import ack, approvals, config, notify
from oncall.record import load as load_record
from oncall.ui import auth, data

INCIDENT = re.compile(r"^[\w.-]{1,80}$")
SIGNALS = ("error_rate", "p95_ms", "silent")
LOG_NAME = "ui_started.log"
TESTS = {"email": notify.send_test, "call": notify.send_test_call, "telegram": notify.send_test_telegram,
         "slack": notify.send_test_slack}


def _spawn(*args):
    """Run `python -m oncall ...` on its own: a fixed argument list, no shell, output to a log file."""
    config.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    with open(config.RUNS_DIR / LOG_NAME, "ab") as log:
        subprocess.Popen([sys.executable, "-m", "oncall", *args], cwd=config.ROOT, stdin=subprocess.DEVNULL,
                         stdout=log, stderr=subprocess.STDOUT)


def _bad(message, status=400):
    return status, {"error": message}


def _password_ok(user, body):
    return auth.check_password(user["user"], body.get("password"))


def acknowledge(user, body):
    incident = body.get("incident")
    if not isinstance(incident, str) or not INCIDENT.match(incident):
        return _bad("that is not an incident id")
    ack.acknowledge(incident, f"{user['user']} (dashboard)")
    return 200, {"message": f"{incident} acknowledged"}


def decide(user, body):
    """Approve or reject the exact step the person was shown, if the agent is still waiting on it."""
    if body.get("decision") not in ("approved", "rejected"):
        return _bad("decision must be approved or rejected")
    if not _password_ok(user, body):
        return _bad("that password is not correct", 403)
    path = data._find(body.get("run_id") or "")
    if path is None:
        return _bad("no such run", 404)
    record = load_record(path)
    steps = (record.get("remediation_plan") or {}).get("steps") or []
    step = next((s for s in steps if s.get("step") == body.get("step")), None)
    if (record.get("status") != "running" or step is None
            or (step.get("approval") or {}).get("status") != "awaiting"):
        return _bad("this step is no longer waiting for a decision", 409)
    if (step.get("action"), step.get("target")) != (body.get("action"), body.get("target")):
        return _bad("this step changed after you opened it; check it again", 409)
    if not approvals.submit(record["metadata"]["run_id"], step, body["decision"], user["user"], "dashboard"):
        return _bad("someone has already decided this step", 409)
    return 200, {"message": f"step {step['step']} {body['decision']}"}


def stop(user, body):
    config.KILL_SWITCH_FILE.write_text("stop\n")
    return 200, {"message": "kill switch ON: P0ppy is stopped"}


def resume(user, body):
    if not _password_ok(user, body):
        return _bad("that password is not correct", 403)
    config.KILL_SWITCH_FILE.unlink(missing_ok=True)
    return 200, {"message": "kill switch off: P0ppy can act again"}


def investigate(user, body):
    service, signal, live = body.get("service"), body.get("signal", "error_rate"), body.get("execute") is True
    if service not in config.CRITICALITY or signal not in SIGNALS:
        return _bad("choose a known service and signal")
    if live and not auth.can(user["role"], "approver"):
        return _bad("only an approver can start a live run", 403)
    if live and not _password_ok(user, body):
        return _bad("that password is not correct", 403)
    if config.KILL_SWITCH_FILE.exists():
        return _bad("the kill switch is on", 409)
    if str(data.status().get("agent", "")).startswith("investigating"):
        return _bad("an investigation is already running", 409)
    _spawn("investigate", "--service", service, "--signal", signal, *(["--execute"] if live else []))
    return 200, {"message": f"investigating {service}" + (" (fixes can run once approved)" if live else " (dry run)")}


def verify(user, body):
    path = data._find(body.get("run_id") or "")
    if path is None:
        return _bad("no such run", 404)
    _spawn("verify", path.stem)
    return 200, {"message": f"checking recovery for {path.stem}"}


def send_test(user, body):
    send = TESTS.get(body.get("channel"))
    if send is None:
        return _bad("unknown channel")
    entry = send()
    if entry.get("status") == "sent":
        return 200, {"message": f"test {body['channel']} sent"}
    return 502, {"error": str(entry.get("error") or "the test failed")[:300]}


# path -> (function, the role it needs)
ROUTES = {"/api/action/ack": (acknowledge, "responder"), "/api/action/investigate": (investigate, "responder"),
          "/api/action/verify": (verify, "responder"), "/api/action/decide": (decide, "approver"),
          "/api/action/stop": (stop, "approver"), "/api/action/resume": (resume, "approver"),
          "/api/action/test": (send_test, "admin")}
