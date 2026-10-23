import pytest

from oncall.agent.diagnosis import validate
from oncall.evidence import Registry, registry
from oncall.sources.guard import ForbiddenAccess, check
from oncall.tools._common import cap, normalise, parse_docker_time, parse_window


def test_registry_ids_and_validation():
    r = Registry()
    a = r.add("t", "metric", "one")
    b = r.add("t", "log", "two", {"x": 1})
    assert (a, b) == ("E1", "E2")
    assert r.validate(["E2", "E9"]) == (["E2"], ["E9"])
    r.reset()
    assert len(r) == 0 and r.add("t", "metric", "again") == "E1"


@pytest.mark.parametrize("target", [
    "http://localhost:8080/feature", "http://localhost:8080/feature/api/x",
    "http://localhost:8080/flagservice/ofrep/v1", "http://localhost:8013/x",
    "http://localhost:8016", "/demo/src/flagd/demo.flagd.json", "docker logs flagd",
])
def test_guard_blocks_flag_access(target):
    with pytest.raises(ForbiddenAccess):
        check(target)


def test_guard_allows_telemetry():
    for url in ("http://localhost:9090/api/v1/query", "http://localhost:8080/jaeger/ui/api/services"):
        assert check(url) == url


def test_parse_window():
    assert parse_window("5m") == 300
    assert parse_window("90s") == 90
    assert parse_window(120) == 120
    assert parse_window(None, default=7) == 7
    with pytest.raises(ValueError):
        parse_window("five")


def test_normalise_groups_similar_messages():
    a = normalise("timeout after 5003ms talking to 10.0.0.12:8080 id 3f2a9c1d-1111-2222-3333-444455556666")
    b = normalise("timeout after 12ms talking to 10.0.0.99:8080 id 0a2a9c1d-1111-2222-3333-444455556666")
    assert a == b


def test_cap_trims_lists():
    out = cap({"rows": [{"x": "y" * 100} for _ in range(100)]}, max_bytes=1000)
    assert out["truncated"] and len(out["rows"]) < 100


def test_parse_docker_time():
    assert parse_docker_time("0001-01-01T00:00:00Z") is None
    t = parse_docker_time("2026-10-23T07:10:00.500000000Z")
    assert t == pytest.approx(1792739400.5)


def test_validate_drops_invented_evidence():
    registry.reset()
    real = registry.add("t", "trace", "checkout's Charge call to payment got no server span")
    s = validate({
        "headline": "h",
        "affected_services": [{"service": "checkout", "impact": "i", "evidence": [real, "E99"]}],
        "anomalies": [{"service": "cart", "signal": "made up", "observed": "1", "baseline": "0",
                       "evidence": ["E42"]}],
        "suspected_origin": {"service": "payment", "reasoning": "r", "confidence": 1.7,
                             "evidence": [real]},
        "open_questions": [],
    })
    assert s["affected_services"][0]["evidence"] == [real]
    assert s["anomalies"] == []
    assert s["validation"]["dropped_evidence_ids"] == ["E42", "E99"]
    # Origin-only input: a root cause is synthesised from it (fault class unknown) and diagnosed.
    assert s["suspected_origin"]["confidence"] == 1.0 and s["status"] == "diagnosed"
    assert s["suspected_origin"]["evidence_detail"][real].startswith("checkout's Charge")
    assert s["root_causes"][0]["component"] == "payment" and s["proposed_action"]["dry_run"] is True


def test_load_dotenv(tmp_path, monkeypatch):
    from oncall.config import load_dotenv
    env = tmp_path / ".env"
    env.write_text('# comment\nONCALL_T1="from-file"\nexport ONCALL_T2=two\nONCALL_T3=file\nBROKEN\n')
    monkeypatch.delenv("ONCALL_T1", raising=False)
    monkeypatch.delenv("ONCALL_T2", raising=False)
    monkeypatch.setenv("ONCALL_T3", "real-env-wins")
    load_dotenv(env)
    import os
    assert os.environ["ONCALL_T1"] == "from-file" and os.environ["ONCALL_T2"] == "two"
    assert os.environ["ONCALL_T3"] == "real-env-wins"
    monkeypatch.delenv("ONCALL_T1")
    monkeypatch.delenv("ONCALL_T2")
