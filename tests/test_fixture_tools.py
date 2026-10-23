"""Each tool against the payment_down fixtures (real response shapes, synthetic numbers)."""
import pytest

from oncall.evidence import registry
from oncall.sources import SourceError
from oncall.tools.deploys import get_deploy_history
from oncall.tools.logs import search_logs
from oncall.tools.metrics import get_metric, get_service_health, list_metrics
from oncall.tools.traces import find_error_traces


def test_service_health_ranks_checkout_and_finds_silent_services(payment_down):
    out = get_service_health("5m")
    assert out["baseline"] == "loaded"
    top = out["flagged"][0]
    assert top["service"] == "checkout"
    assert "errors" in top["flags"] and top["error_rate"] == pytest.approx(0.9524, abs=1e-3)
    silent = {r["service"] for r in out["flagged"] if "silent" in r["flags"]}
    assert silent == {"payment", "email"}
    assert "cart" in out["healthy"]
    text = registry.get(top["evidence"])["summary"]
    assert "95.2%" in text and "baseline 0.0%" in text


def test_get_metric_summarises_range(payment_down):
    out = get_metric("container_memory_percent_ratio", window="15m")
    by_name = {s["labels"]["container_name"]: s for s in out["series"]}
    assert by_name["payment"]["points"] < by_name["checkout"]["points"]  # samples stop at the stop
    assert by_name["payment"]["trend"] == "rising"
    assert all(s["evidence"] in registry for s in out["series"])
    assert list_metrics("jvm gc")["names"] == ["jvm_gc_duration_seconds_bucket"]


def test_find_error_traces_points_at_payment(payment_down):
    out = find_error_traces("checkout", lookback="15m", limit=20)
    assert out["traces_examined"] == 5  # healthy traces are filtered by errors_only
    origin = out["origins"][0]
    assert origin["origin_service"] == "checkout"
    assert origin["operation"] == "oteldemo.PaymentService/Charge"
    assert origin["unanswered_call_to"] == "oteldemo.PaymentService"
    assert "zero addresses" in origin["message"]
    assert origin["chain"] == ["frontend-proxy", "frontend", "checkout"]
    assert "got no server span" in registry.get(origin["evidence"])["summary"]


def test_no_recent_error_traces_for_payment(payment_down):
    out = find_error_traces("payment")
    assert out["traces_examined"] == 0 and out["evidence"] in registry


def test_search_logs_groups_checkout_errors(payment_down):
    out = search_logs("checkout", since="5m", level="warn")
    top = out["patterns"][0]
    assert top["service"] == "checkout" and top["level"] == "error" and top["count"] == 6
    assert "name resolver error" in top["example"]
    assert out["services"]["checkout"]["matching_lines"] == 6


def test_search_logs_payment_quiet_and_unknown_container(payment_down):
    out = search_logs(["payment", "nosuch"], since="5m", level="info")
    assert out["services"]["payment"]["matching_lines"] == 16
    assert "No such container" in out["services"]["nosuch"]["error"]


def test_deploy_history_reports_stopped_payment(payment_down):
    out = get_deploy_history()
    first = out["changed_or_unhealthy"][0]
    assert first["name"] == "payment" and first["status"] == "exited"
    assert first["exit_code"] == 143 and first["stopped_ago_s"] == 99
    assert first["oom_killed"] is False and first["restart_count"] == 0
    assert "not running (exited, exit code 143, stopped 99s ago)" in registry.get(first["evidence"])["summary"]
    assert len(out["changed_or_unhealthy"]) == 1 and out["stats_available"]
    assert "cart" in out["healthy"]
    assert [m["name"] for m in out["top_memory"][:2]] == ["ad", "cart"]  # 70% and 62% of limit


def test_flag_containers_are_never_listed(payment_down):
    assert "flagd" not in payment_down.container_names()
    with pytest.raises(Exception, match="HARD RULE 2"):
        payment_down.docker_inspect(["flagd"])
    with pytest.raises(SourceError):
        payment_down.docker_inspect(["nosuch"])
