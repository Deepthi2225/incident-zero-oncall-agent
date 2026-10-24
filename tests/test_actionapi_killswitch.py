"""The Action API enforces the kill switch itself (based on Padma's checkpoint3 branch): HTTP 423 on
every action request while STOP exists, and the agent treats 423 as a refusal, not a retryable error."""
import json
import threading

import pytest

from actionapi import config as api_config
from actionapi.server import make_server
from actionapi.service import ActionService
from tests.test_actionapi import TOKEN, Clock, FakeDocker, FakeLocust, audit, call


@pytest.fixture
def stop(tmp_path):
    # a different file from the agent's own switch (tmp_path / "STOP" in conftest), so the tests
    # prove the API's 423 path rather than the agent refusing first
    return tmp_path / "API_STOP"


@pytest.fixture
def api(tmp_path, stop):
    svc = ActionService(token=TOKEN, docker=FakeDocker(), locust=FakeLocust(), clock=Clock(),
                        audit_log=tmp_path / "action_api.jsonl", cooldown_s=60, kill_switch=stop)
    svc.requests = 0
    return svc


def test_every_action_request_gets_423_while_stop_exists(api, stop):
    assert call(api, "/actions/restart_service", {"service": "cart"}, key="k1")[0] == 200
    stop.write_text("stop")
    for path, body, key in [("/actions/restart_service", {"service": "payment"}, None),
                            ("/actions/restart_service", {"service": "payment", "dry_run": True}, None),
                            ("/actions/restart_service", {"service": "cart"}, "k1"),  # replay too
                            ("/actions/throttle_load_generator", {"users": 2}, None),
                            ("/actions/unthrottle_load_generator", {}, None),
                            ("/actions/toggle_feature_flag", {"flag": "x"}, None)]:
        status, resp = call(api, path, body, key=key)
        assert (status, resp["error"]) == (423, "kill switch is on"), path
    assert api.docker.restarts == ["cart"] and api.locust.set_calls == []
    assert call(api, "/health", method="GET", token=None)[0] == 200  # liveness still answers
    entries = audit(api)
    assert [e["status"] for e in entries] == [200, 423, 423, 423, 423, 423, 423, 200]
    assert entries[1]["params"] == {"service": "payment"} and entries[2]["dry_run"] is True


def test_normal_behaviour_without_the_stop_file(api, stop):
    assert not stop.exists()
    assert call(api, "/actions/restart_service", {"service": "payment"})[0] == 200
    stop.write_text("stop")
    stop.unlink()  # python -m oncall resume
    assert call(api, "/actions/restart_service", {"service": "cart"})[0] == 200


def test_default_stop_path_is_the_agents_without_importing_oncall():
    source = (api_config.ROOT / "actionapi" / "config.py").read_text(encoding="utf-8")
    assert "import oncall" not in source and "from oncall" not in source
    from oncall import config as agent_config
    assert api_config.KILL_SWITCH_FILE == agent_config.KILL_SWITCH_FILE  # same env var, same default


@pytest.fixture
def server(api, mem):
    srv = make_server(api, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield api, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_client_treats_423_as_a_refusal_not_a_retry(server, stop):
    from oncall.action_api import ActionApiError, HttpActionClient, KillSwitchRefused
    api, url = server
    stop.write_text("stop")
    with pytest.raises(KillSwitchRefused, match="refused: kill switch is on"):
        HttpActionClient(url, token=TOKEN).call("restart_service", "payment", idempotency_key="r:1")
    assert issubclass(KillSwitchRefused, ActionApiError)
    assert len(audit(api)) == 1  # sent once, not retried


def test_executor_logs_423_as_refused_for_actions_and_rollbacks(server, stop):
    from oncall import config
    from oncall.action_api import HttpActionClient
    from oncall.remediate import run_plan
    from oncall.verify import NOT_RECOVERED
    from tests.test_remediate import StubVerifier, human, logged, plan_for
    api, url = server
    assert not config.KILL_SWITCH_FILE.exists()  # only the API's switch is on

    stop.write_text("stop")
    diag = plan_for(("payment", "container_stopped_or_restarting"))
    plan = run_plan(diag, HttpActionClient(url, token=TOKEN), human({}), StubVerifier({}), "r1", log=lambda s: None)
    step = plan["steps"][0]
    assert step["execution"]["status"] == "refused" and "kill switch" in step["execution"]["reason"]
    assert step["outcome"] == "refused" and plan["outcome"] == "stopped" and api.docker.restarts == []
    assert step["kill_switch"] == {"before_approval": "off", "before_action": "off"}  # agent's switch off
    assert json.loads(api.audit_log.read_text().splitlines()[-1])["status"] == 423  # the API refused
    assert logged()[-1]["event"] == "refused" and "kill switch" in logged()[-1]["reason"]

    stop.unlink()
    diag = plan_for(("frontend-proxy", "traffic_flood"))

    class SwitchOnDuringVerify(StubVerifier):
        def verify(self, step, services, before=None, progress=None):
            stop.write_text("stop")  # an operator stops the API mid-incident
            return super().verify(step, services, before)

    plan = run_plan(diag, HttpActionClient(url, token=TOKEN), human({}),
                    SwitchOnDuringVerify({"load-generator": NOT_RECOVERED}), "r2", log=lambda s: None)
    assert plan["steps"][0]["rollback_result"]["status"] == "refused"
    assert logged()[-1]["event"] == "rollback_refused" and api.locust.set_calls == [2]
    assert json.loads(api.audit_log.read_text().splitlines()[-1])["status"] == 423
