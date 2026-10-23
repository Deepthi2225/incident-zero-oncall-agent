"""Flag data never survives (live answers, bundles, fixtures); search_logs prefers OpenSearch."""
import json

from oncall.sources.bundle import BundleSource, RecordingSource
from oncall.sources.sanitize import contains_flags, strip_flags
from oncall.tools.logs import records_to_text, search_logs
from tests.conftest import FIXTURES


def test_strip_flags_removes_labels_tags_events_and_log_lines():
    data = {
        "metric": {"service_name": "cart", "feature_flag_key": "x"},
        "tags": [{"key": "feature_flag.key", "value": "cartFailure"}, {"key": "rpc.method", "value": "m"}],
        "events": [{"name": "feature_flag.evaluation"}, {"name": "ok"}],
        "logs": [{"fields": [{"key": "event", "value": "feature_flag"}]}, {"fields": [{"key": "event", "value": "x"}]}],
        "text": "line one\nevaluated feature_flag paymentFailure=on\nline three",
    }
    clean = strip_flags(data)
    assert not contains_flags(clean)
    assert clean["metric"] == {"service_name": "cart"}
    assert clean["tags"] == [{"key": "rpc.method", "value": "m"}]
    assert clean["events"] == [{"name": "ok"}] and len(clean["logs"]) == 1
    assert clean["text"] == "line one\nline three"


def test_committed_fixtures_contain_no_real_flag_data():
    for path in FIXTURES.rglob("*.json"):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "feature_flag" in line.lower():
                assert "SYNTHETIC" in line or "feature_flag.evaluation" in line or '"key": "feature_flag.key"' in line, \
                    f"{path.name}: {line.strip()[:80]}"
        assert "cartMetricCardinalityOverflow" not in text and "paymentFailure" not in text


def test_recording_never_stores_flag_data(mem, tmp_path):
    mem.logs["cart"] = "ok\nfeature_flag cartFailure on\nstill ok"
    rec = RecordingSource(mem, tmp_path / "b")
    rec.docker_logs("cart", 300)
    stored = (tmp_path / "b" / "docker_logs.json").read_text()
    assert "feature_flag" not in stored and "still ok" in stored
    assert BundleSource(tmp_path / "b").docker_logs("cart", 300) == "ok\nstill ok"


def test_records_to_text_levels_and_order():
    docs = [  # newest first, as OpenSearch returns them
        {"ts": "2026-10-23T10:00:02.0Z", "severity_number": 17, "body": "charge failed"},
        {"ts": "2026-10-23T10:00:01.0Z", "severity_number": None, "severity_text": None, "body": {"k": 1}},
    ]
    assert records_to_text(docs).splitlines() == [
        '2026-10-23T10:00:01.0Z INFO: {"k": 1}', "2026-10-23T10:00:02.0Z ERROR: charge failed"]


def test_search_logs_prefers_opensearch_and_falls_back(mem, monkeypatch):
    calls = []

    def opensearch_logs(service, since_s, min_severity=0, size=500):
        calls.append((service, min_severity))
        if service == "checkout":
            return [{"ts": "2026-10-23T10:00:0%dZ" % i, "severity_number": 17,
                     "body": "failed to charge card: name resolver error"} for i in range(3)]
        return [] if service == "cart" else None

    mem.opensearch_logs = opensearch_logs
    mem.logs["cart"] = "2026-10-23T10:00:00.0Z fail: Wasn't able to connect to redis"
    mem.logs["email"] = "2026-10-23T10:00:00.0Z ERROR boom"
    out = search_logs(["checkout", "cart", "email"], since="5m", level="warn")
    s = out["services"]
    assert s["checkout"]["log_source"] == "opensearch" and s["checkout"]["matching_lines"] == 3
    assert s["cart"]["log_source"] == "opensearch (none) + docker logs"
    assert s["email"]["log_source"] == "docker logs"  # OpenSearch unavailable
    assert ("checkout", 13) in calls  # warn -> OTel severity 13
    top = next(p for p in out["patterns"] if p["service"] == "checkout")
    assert top["count"] == 3 and top["level"] == "error"
    assert json.dumps(out).count("opensearch") >= 2
