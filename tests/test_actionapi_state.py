"""The Action API keeps its state on disk (based on Padma's checkpoint3 branch): an unthrottle still
works after an API restart, idempotent replays and cooldowns survive, the file is written atomically."""
import json

from actionapi.service import ActionService
from tests.test_actionapi import TOKEN, Clock, FakeDocker, FakeLocust, call


def service(tmp_path, locust=None, docker=None, clock=None):
    svc = ActionService(token=TOKEN, docker=docker or FakeDocker(), locust=locust or FakeLocust(),
                        clock=clock or Clock(), audit_log=tmp_path / "action_api.jsonl", cooldown_s=60,
                        state_file=tmp_path / "action_api_state.json", kill_switch=tmp_path / "API_STOP")
    svc.requests = 0
    return svc


def test_unthrottle_works_after_an_api_restart(tmp_path):
    locust = FakeLocust(users=10)
    first = service(tmp_path, locust=locust)
    assert call(first, "/actions/throttle_load_generator", {"users": 2})[0] == 200
    state = json.loads((tmp_path / "action_api_state.json").read_text())
    assert state["previous_users"] == 10

    restarted = service(tmp_path, locust=locust)  # the API process was restarted
    status, resp = call(restarted, "/actions/unthrottle_load_generator")
    assert status == 200 and resp["users_now"] == 10 and locust.set_calls == [2, 10]
    assert json.loads((tmp_path / "action_api_state.json").read_text())["previous_users"] is None


def test_idempotent_replay_and_cooldown_survive_a_restart(tmp_path):
    docker, clock = FakeDocker(), Clock()
    first = service(tmp_path, docker=docker, clock=clock)
    assert call(first, "/actions/restart_service", {"service": "payment"}, key="r1:step1:restart_service")[0] == 200

    restarted = service(tmp_path, docker=docker, clock=clock)
    status, resp = call(restarted, "/actions/restart_service", {"service": "payment"}, key="r1:step1:restart_service")
    assert status == 200 and resp["replayed"] is True and docker.restarts == ["payment"]  # not applied twice
    assert call(restarted, "/actions/restart_service", {"service": "payment"})[0] == 429  # cooldown kept


def test_dry_runs_and_refusals_do_not_write_state(tmp_path):
    svc = service(tmp_path)
    call(svc, "/actions/restart_service", {"service": "payment", "dry_run": True})
    call(svc, "/actions/restart_service", {"service": "prometheus"})
    assert not (tmp_path / "action_api_state.json").exists()


def test_atomic_write_leaves_no_temp_file_and_a_corrupt_file_starts_clean(tmp_path):
    svc = service(tmp_path)
    call(svc, "/actions/restart_service", {"service": "payment"})
    assert sorted(p.name for p in tmp_path.iterdir()) == ["action_api.jsonl", "action_api_state.json"]
    (tmp_path / "action_api_state.json").write_text('{"previous_users": 4, "last_')  # half-written
    fresh = service(tmp_path)
    assert fresh.previous_users is None and fresh.idempotency == {} and fresh.last_action == {}
    assert call(fresh, "/actions/unthrottle_load_generator")[0] == 409  # nothing remembered to undo
