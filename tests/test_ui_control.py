"""Signing in, roles, and every dashboard action, through a real local server."""
import http.client
import json
import threading

import pytest

from oncall import ack, approvals, config
from oncall.remediate import make_approver
from oncall.ui import actions, auth, server as srv

PASSWORD = "correct-horse-battery"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UI_USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", tmp_path / "STOP")
    monkeypatch.setattr(srv, "sessions", auth.Sessions())
    monkeypatch.setattr(srv, "throttle", auth.Throttle())
    spawned = []
    monkeypatch.setattr(actions, "_spawn", lambda *a: spawned.append(a))
    for name in auth.ROLES:
        auth.add_user(name, PASSWORD, name)
    server = srv.make_server(port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield type("App", (), {"port": server.server_address[1], "spawned": spawned, "dir": tmp_path})
    server.shutdown()
    server.server_close()


class Client:
    def __init__(self, app, host=None):
        self.port, self.cookie, self.csrf = app.port, None, ""
        self.host = host or f"127.0.0.1:{app.port}"

    def call(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": self.host, **({"Cookie": self.cookie} if self.cookie else {})}
        data = None
        if method == "POST":
            data = json.dumps(body or {})
            h.update({"Content-Type": "application/json", "X-CSRF-Token": self.csrf})
        h.update(headers or {})
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        self.last = r
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw.decode()

    def login(self, name, password=PASSWORD):
        status, _ = self.call("POST", "/api/login", {"name": name, "password": password})
        if status == 200:
            self.cookie = self.last.getheader("Set-Cookie").split(";")[0]
            self.csrf = self.call("GET", "/api/me")[1]["csrf"]
        return status


def signed_in(app, role):
    client = Client(app)
    assert client.login(role) == 200
    return client


def running_record(app, status="awaiting"):
    step = {"step": 1, "action": "restart_service", "target": "payment", "approval": {"status": status}}
    record = {"schema_version": 2, "status": "running", "metadata": {"run_id": "20991231-000001", "incident_id": "INC-1", "mode": "live"},
              "remediation_plan": {"steps": [step]}}
    (app.dir / "20991231-000001.json").write_text(json.dumps(record), encoding="utf-8")
    return {"run_id": "20991231-000001", "step": 1, "action": "restart_service", "target": "payment", "decision": "approved", "password": PASSWORD}


# --- accounts ----------------------------------------------------------------------------------------

def test_passwords_are_hashed_and_rules_enforced(app):
    assert PASSWORD not in config.UI_USERS_FILE.read_text()
    with pytest.raises(ValueError, match="at least"):
        auth.add_user("x", "short", "viewer")
    with pytest.raises(ValueError, match="role"):
        auth.add_user("x", PASSWORD, "king")
    with pytest.raises(ValueError):
        auth.add_user("../evil", PASSWORD, "viewer")


# --- signing in --------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/api/incidents", "/api/status", "/api/settings", "/api/report/x", "/api/audit"])
def test_nothing_is_readable_without_signing_in(app, path):
    assert Client(app).call("GET", path)[0] == 401


def test_the_page_redirects_to_login(app):
    client = Client(app)
    assert client.call("GET", "/")[0] == 302 and client.last.getheader("Location") == "/login"


def test_cookie_is_protected(app):
    client = Client(app)
    client.call("POST", "/api/login", {"name": "viewer", "password": PASSWORD})
    cookie = client.last.getheader("Set-Cookie")
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie


def test_wrong_password_and_lockout(app):
    client = Client(app)
    for _ in range(auth.MAX_FAILURES):
        assert client.login("admin", "wrong-password-here") == 401
    assert client.login("admin") == 429  # even the right password, during the lockout


def test_an_unknown_name_fails_like_a_wrong_password(app):
    assert Client(app).login("nobody") == 401


def test_signing_out_ends_the_session(app):
    client = signed_in(app, "viewer")
    assert client.call("POST", "/api/logout")[0] == 200
    assert client.call("GET", "/api/status")[0] == 401


def test_removing_an_account_ends_its_session_at_once(app):
    client = signed_in(app, "viewer")
    auth.remove_user("viewer")
    assert client.call("GET", "/api/status")[0] == 401


# --- requests from elsewhere --------------------------------------------------------------------------

def test_a_change_without_the_csrf_token_is_refused(app):
    client = signed_in(app, "admin")
    client.csrf = "wrong"
    assert client.call("POST", "/api/action/stop")[0] == 403 and not config.KILL_SWITCH_FILE.exists()


def test_another_site_is_refused(app):
    client = signed_in(app, "admin")
    assert client.call("POST", "/api/action/stop", headers={"Origin": "http://evil.test"})[0] == 403
    assert not config.KILL_SWITCH_FILE.exists()


def test_dns_rebinding_host_is_refused(app):
    client = signed_in(app, "admin")
    client.host = "evil.test"
    assert client.call("GET", "/api/status")[0] == 403 and client.call("POST", "/api/action/stop")[0] == 403


@pytest.mark.parametrize("headers", [{"Content-Type": "text/plain"}, {"Content-Length": str(srv.MAX_BODY + 1)}])
def test_only_small_json_is_accepted(app, headers):
    client = signed_in(app, "admin")
    assert client.call("POST", "/api/action/stop", headers=headers)[0] == 400


# --- roles ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("role,path,allowed", [("viewer", "ack", False), ("responder", "ack", True), ("responder", "stop", False),
                                               ("approver", "stop", True), ("approver", "test", False), ("viewer", "decide", False)])
def test_roles_are_enforced(app, role, path, allowed):
    client = signed_in(app, role)
    status, _ = client.call("POST", f"/api/action/{path}", {"incident": "INC-1", "channel": "none"})
    assert (status != 403) is allowed


def test_a_viewer_cannot_read_the_audit_log(app):
    assert signed_in(app, "viewer").call("GET", "/api/audit")[0] == 403
    assert signed_in(app, "approver").call("GET", "/api/audit")[0] == 200


# --- approving a fix ------------------------------------------------------------------------------------

def test_approving_hands_over_the_exact_step(app):
    body = running_record(app)
    assert signed_in(app, "approver").call("POST", "/api/action/decide", body)[0] == 200
    found = approvals.take(body["run_id"], {"step": 1, "action": "restart_service", "target": "payment"})
    assert found["decision"] == "approved" and found["by"] == "approver"


def test_approving_needs_the_password_again(app):
    body = {**running_record(app), "password": "not-my-password"}
    assert signed_in(app, "approver").call("POST", "/api/action/decide", body)[0] == 403
    assert not list((app.dir / "approvals").glob("*")) if (app.dir / "approvals").exists() else True


def test_a_step_that_changed_cannot_be_approved(app):
    body = {**running_record(app), "target": "checkout"}
    assert signed_in(app, "approver").call("POST", "/api/action/decide", body)[0] == 409


def test_a_step_not_waiting_cannot_be_approved(app):
    body = running_record(app, status="approved")
    assert signed_in(app, "approver").call("POST", "/api/action/decide", body)[0] == 409


def test_the_first_decision_wins(app):
    body = running_record(app)
    client = signed_in(app, "approver")
    assert client.call("POST", "/api/action/decide", body)[0] == 200
    assert client.call("POST", "/api/action/decide", {**body, "decision": "rejected"})[0] == 409


def test_a_decision_for_another_step_is_ignored(app):
    step = {"step": 1, "action": "restart_service", "target": "payment"}
    approvals.submit("R", step, "approved", "a")
    assert approvals.take("R", {**step, "target": "cart"}) is None


def test_the_password_never_reaches_the_audit_log(app):
    body = running_record(app)
    signed_in(app, "approver").call("POST", "/api/action/decide", body)
    text = (app.dir / "ui_audit.jsonl").read_text()
    assert "decide" in text and PASSWORD not in text


# --- the agent's side ------------------------------------------------------------------------------------

STEP = {"step": 1, "action": "restart_service", "target": "payment"}
DIAG = {"status": "diagnosed", "confidence": 0.9, "remediation_plan": {"steps": [STEP]}}


@pytest.fixture
def waiting(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", tmp_path / "STOP")
    monkeypatch.setattr(config, "UI_APPROVALS", True)
    monkeypatch.setattr(config, "APPROVAL_TIMEOUT_S", 3.0)
    return make_approver(interactive=True, run_id=lambda: "R")


def test_a_dashboard_approval_counts_as_a_human_and_names_them(waiting):
    approvals.submit("R", STEP, "approved", "deepthi")
    result = waiting(STEP, DIAG)
    assert result["status"] == "approved" and result["approved_by"] == "human" and "deepthi" in result["notes"]


def test_a_dashboard_rejection_is_a_rejection(waiting):
    approvals.submit("R", STEP, "rejected", "deepthi")
    assert waiting(STEP, DIAG)["status"] == "rejected"


def test_nobody_answering_is_never_an_approval(waiting, monkeypatch):
    monkeypatch.setattr(config, "APPROVAL_TIMEOUT_S", 0.2)
    assert waiting(STEP, DIAG)["status"] == "pending"


def test_the_kill_switch_ends_the_wait(waiting):
    config.KILL_SWITCH_FILE.write_text("stop")
    assert waiting(STEP, DIAG)["status"] == "pending"


@pytest.mark.parametrize("typed,expected", [("y", "approved"), ("", "rejected"), ("nope", "rejected")])
def test_the_terminal_still_answers_and_enter_means_no(waiting, monkeypatch, typed, expected):
    from oncall import remediate
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr(remediate, "_start_terminal_reader", lambda: None)
    threading.Timer(0.1, remediate._terminal_lines.put, [typed]).start()
    assert waiting(STEP, DIAG)["status"] == expected


def test_auto_approve_never_uses_the_dashboard(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UI_APPROVALS", True)
    assert make_approver(auto_approve=True, run_id=lambda: "R")(STEP, DIAG)["approved_by"] == "auto"


def test_dashboard_off_keeps_the_old_behaviour(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert make_approver(interactive=True, run_id=lambda: "R")(STEP, DIAG)["status"] == "pending"


# --- other actions --------------------------------------------------------------------------------------

def test_kill_switch_stop_and_resume(app):
    client = signed_in(app, "approver")
    assert client.call("POST", "/api/action/stop")[0] == 200 and config.KILL_SWITCH_FILE.exists()
    assert client.call("POST", "/api/action/resume", {"password": "wrong"})[0] == 403 and config.KILL_SWITCH_FILE.exists()
    assert client.call("POST", "/api/action/resume", {"password": PASSWORD})[0] == 200 and not config.KILL_SWITCH_FILE.exists()


def test_acknowledge_records_who(app):
    signed_in(app, "responder").call("POST", "/api/action/ack", {"incident": "INC-7"})
    assert "INC-7" in ack.acknowledged() and "responder" in (app.dir / "acks.jsonl").read_text()


def test_investigate_is_dry_run_with_fixed_arguments(app):
    assert signed_in(app, "responder").call("POST", "/api/action/investigate", {"service": "payment", "signal": "error_rate"})[0] == 200
    assert app.spawned == [("investigate", "--service", "payment", "--signal", "error_rate")]


@pytest.mark.parametrize("service", ["../etc", "payment; rm -rf /", "--execute", None])
def test_investigate_rejects_unknown_services(app, service):
    assert signed_in(app, "responder").call("POST", "/api/action/investigate", {"service": service})[0] == 400 and not app.spawned


def test_a_live_run_needs_an_approver_and_their_password(app):
    body = {"service": "payment", "execute": True, "password": PASSWORD}
    assert signed_in(app, "responder").call("POST", "/api/action/investigate", body)[0] == 403
    client = signed_in(app, "approver")
    assert client.call("POST", "/api/action/investigate", {**body, "password": "nope"})[0] == 403 and not app.spawned
    assert client.call("POST", "/api/action/investigate", body)[0] == 200 and "--execute" in app.spawned[0]


def test_investigate_is_refused_while_the_kill_switch_is_on(app):
    config.KILL_SWITCH_FILE.write_text("stop")
    assert signed_in(app, "responder").call("POST", "/api/action/investigate", {"service": "payment"})[0] == 409


def test_verify_needs_a_real_run(app):
    client = signed_in(app, "responder")
    assert client.call("POST", "/api/action/verify", {"run_id": "../../etc"})[0] == 404
    running_record(app)
    assert client.call("POST", "/api/action/verify", {"run_id": "20991231-000001"})[0] == 200 and app.spawned


def test_settings_never_show_secrets(app, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123456:SECRET-TOKEN-VALUE")
    status, found = signed_in(app, "viewer").call("GET", "/api/settings")
    assert status == 200 and "SECRET-TOKEN-VALUE" not in json.dumps(found)
