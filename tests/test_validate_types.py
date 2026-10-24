"""validate() never crashes on wrong types from the model (live crash: suspected_origin as a string)."""
from oncall.agent.diagnosis import validate
from oncall.evidence import registry


def test_suspected_origin_as_a_string(mem):
    registry.reset()
    eid = registry.add("t", "container", "payment exited")
    d = validate({"headline": "h", "affected_services": "checkout", "anomalies": None,
                  "root_causes": [{"component": "payment", "fault_class": "container_stopped_or_restarting",
                                   "cause": "exited", "confidence": 0.9, "evidence": eid}, "not an object"],
                  "suspected_origin": "payment", "proposed_action": "restart_service",
                  "prevention": "alert on exits", "open_questions": None})
    assert d["status"] == "diagnosed" and d["root_causes"][0]["evidence"] == [eid]
    assert d["suspected_origin"]["service"] == "payment"
    assert d["prevention"] == ["alert on exits"]
    assert any("suspected_origin was a string" in n for n in d["validation"]["notes"])
    assert validate({"suspected_origin": 42, "root_causes": 7})["status"] == "escalate"  # nonsense: no crash
