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
    monkeypatch.setattr(auth, "step_up", auth.Throttle())
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


# --- the whole approval path ------------------------------------------------------------------------------

def test_a_dashboard_approval_reaches_a_running_agent(payment_down, app, monkeypatch):
    import time

    from tests.conftest import FIXTURES
    from tests.test_agent_loop import block_tool, make_agent, response, submit
    from tests.test_remediate import StubVerifier
    monkeypatch.setattr(config, "UI_APPROVALS", True)
    monkeypatch.setattr(config, "APPROVAL_TIMEOUT_S", 30.0)

    def script(n, kwargs):
        if n == 1:
            return response(block_tool("d", "get_deploy_history", {"lookback": "30m"}))
        found = json.loads(kwargs["messages"][-1]["content"][0]["content"])
        return response(submit("s", "payment", [found["changed_or_unhealthy"][0]["evidence"]]))

    agent, _ = make_agent(monkeypatch, script, verifier=StubVerifier({}))
    alert = json.loads((FIXTURES / "payment_down" / "alert.json").read_text())
    done = {}
    worker = threading.Thread(target=lambda: done.update(agent.run(alert)), daemon=True)
    worker.start()
    client = signed_in(app, "approver")
    for _ in range(100):
        status, record = client.call("GET", "/api/incident")
        if status == 200 and record["ui"]["awaiting_approval"]:
            break
        time.sleep(0.1)
    assert record["ui"]["awaiting_approval"] == [1]
    step = record["remediation_plan"]["steps"][0]
    assert client.call("POST", "/api/action/decide", {"run_id": record["metadata"]["run_id"], "step": 1, "action": step["action"],
                                                      "target": step["target"], "decision": "approved", "password": PASSWORD})[0] == 200
    worker.join(20)
    assert not worker.is_alive()
    approval = done["remediation_plan"]["steps"][0]["approval"]
    assert approval["status"] == "approved" and approval["approved_by"] == "human" and "approver" in approval["notes"]


# --- working as a team --------------------------------------------------------------------------------------

def team(client):
    return client.call("GET", "/api/incident")[1]["ui"]["team"]


def test_claiming_names_the_owner_and_acknowledges(app):
    running_record(app)
    client = signed_in(app, "responder")
    assert client.call("POST", "/api/action/claim", {"incident": "INC-1"})[0] == 200
    assert team(client)["owner"] == "responder" and "INC-1" in ack.acknowledged()


def test_handing_over_only_to_someone_who_can_respond(app):
    running_record(app)
    client = signed_in(app, "responder")
    assert client.call("POST", "/api/action/assign", {"incident": "INC-1", "to": "approver"})[0] == 200
    assert team(client)["owner"] == "approver"
    assert client.call("POST", "/api/action/assign", {"incident": "INC-1", "to": "viewer"})[0] == 400
    assert client.call("POST", "/api/action/assign", {"incident": "INC-1", "to": "nobody"})[0] == 400


def test_notes_are_shared_and_named_and_limited(app):
    running_record(app)
    client, other = signed_in(app, "responder"), signed_in(app, "approver")
    assert client.call("POST", "/api/action/note", {"incident": "INC-1", "text": "  checked the cert  "})[0] == 200
    row = team(other)["timeline"][0]
    assert row["text"] == "checked the cert" and row["by"] == "responder"
    assert client.call("POST", "/api/action/note", {"incident": "INC-1", "text": "   "})[0] == 400
    assert client.call("POST", "/api/action/note", {"incident": "INC-1", "text": "x" * 2001})[0] == 400


def test_a_viewer_can_read_but_not_write_team_notes(app):
    running_record(app)
    assert signed_in(app, "viewer").call("POST", "/api/action/note", {"incident": "INC-1", "text": "hi"})[0] == 403


def test_changing_a_password_needs_the_old_one_and_signs_out_everywhere(app):
    client, second = signed_in(app, "viewer"), signed_in(app, "viewer")
    new = "a-brand-new-password"
    assert client.call("POST", "/api/action/password", {"password": "wrong", "new_password": new})[0] == 403
    assert client.call("POST", "/api/action/password", {"password": PASSWORD, "new_password": "short"})[0] == 400
    assert client.call("POST", "/api/action/password", {"password": PASSWORD, "new_password": new})[0] == 200
    assert second.call("GET", "/api/status")[0] == 401
    assert Client(app).login("viewer") == 401 and Client(app).login("viewer", new) == 200


def test_wrong_retyped_passwords_lock_out(app):
    client = signed_in(app, "approver")
    for _ in range(auth.MAX_FAILURES):
        client.call("POST", "/api/action/resume", {"password": "wrong"})
    config.KILL_SWITCH_FILE.write_text("stop")
    assert client.call("POST", "/api/action/resume", {"password": PASSWORD})[0] == 403


def test_admins_manage_accounts(app):
    admin = signed_in(app, "admin")
    body = {"name": "nirupam", "role": "responder", "new_password": "a-long-enough-password", "password": PASSWORD}
    assert admin.call("POST", "/api/action/user_save", body)[0] == 200 and auth.role_of("nirupam") == "responder"
    assert admin.call("POST", "/api/action/user_save", {"name": "nirupam", "role": "approver", "password": PASSWORD})[0] == 200
    assert auth.role_of("nirupam") == "approver" and auth.check_password("nirupam", "a-long-enough-password")
    assert "nirupam" in str(admin.call("GET", "/api/users")[1])
    assert admin.call("POST", "/api/action/user_remove", {"name": "nirupam", "password": PASSWORD})[0] == 200


def test_only_admins_see_accounts(app):
    assert signed_in(app, "approver").call("GET", "/api/users")[0] == 403
    assert signed_in(app, "approver").call("POST", "/api/action/user_save", {"name": "x"})[0] == 403


def test_an_admin_cannot_lock_themselves_out(app):
    admin = signed_in(app, "admin")
    assert admin.call("POST", "/api/action/user_remove", {"name": "admin", "password": PASSWORD})[0] == 400
    assert admin.call("POST", "/api/action/user_save", {"name": "admin", "role": "viewer", "password": PASSWORD})[0] == 400


def test_new_passwords_never_reach_the_audit_log(app):
    client = signed_in(app, "viewer")
    client.call("POST", "/api/action/password", {"password": PASSWORD, "new_password": "a-secret-new-password"})
    text = (app.dir / "ui_audit.jsonl").read_text()
    assert "a-secret-new-password" not in text and PASSWORD not in text


# --- reached through Tailscale ---------------------------------------------------------------------------------

def test_the_tailscale_name_works_only_when_configured(app, monkeypatch):
    name = "poppy.tail1234.ts.net"
    assert Client(app, host=name).call("GET", "/login")[0] == 403
    monkeypatch.setattr(config, "UI_ALLOWED_HOSTS", [name])
    client = Client(app, host=name)
    assert client.login("admin") == 200
    assert client.call("POST", "/api/action/stop", headers={"Origin": f"https://{name}"})[0] == 200
    assert client.call("POST", "/api/action/stop", headers={"Origin": "https://evil.test"})[0] == 403


def test_the_cookie_is_https_only_through_tailscale(app, monkeypatch):
    name = "poppy.tail1234.ts.net"
    monkeypatch.setattr(config, "UI_ALLOWED_HOSTS", [name])
    client = Client(app, host=name)
    client.call("POST", "/api/login", {"name": "viewer", "password": PASSWORD})
    assert "Secure" in client.last.getheader("Set-Cookie")
    local = Client(app)
    local.call("POST", "/api/login", {"name": "viewer", "password": PASSWORD})
    assert "Secure" not in local.last.getheader("Set-Cookie")
