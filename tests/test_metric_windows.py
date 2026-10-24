"""get_metric widens rate/increase windows below 3m and explains empty results."""
from oncall.evidence import registry
from oncall.tools.metrics import NO_DATA_HINT, get_metric, widen_rate_windows


def test_short_rate_windows_are_widened_to_3m():
    q, notes = widen_rate_windows('sum(rate(calls_total{service_name="cart"}[1m])) / sum(increase(x[90s]))')
    assert q == 'sum(rate(calls_total{service_name="cart"}[3m])) / sum(increase(x[3m]))'
    assert len(notes) == 2 and "rate window [1m] widened to [3m]" in notes[0]
    assert widen_rate_windows("rate(x[5m])") == ("rate(x[5m])", [])          # long enough: unchanged
    assert widen_rate_windows("max_over_time(x[1m])") == ("max_over_time(x[1m])", [])  # not a rate
    assert widen_rate_windows("irate(x[30s])")[0] == "irate(x[3m])"


def test_get_metric_reports_the_widened_query(mem):
    seen = []
    mem.prom_range = lambda query, window_s, step_s: seen.append(query) or [
        {"metric": {"service_name": "cart"}, "values": [[1, "1.0"], [2, "2.0"]]}]
    result = get_metric("rate(calls_total[1m])", window="10m")
    assert seen == ["rate(calls_total[3m])"]
    assert result["promql"] == "rate(calls_total[3m])" and result["asked_promql"] == "rate(calls_total[1m])"
    assert "widened to [3m]" in result["adjusted"][0] and "hint" not in result


def test_empty_result_carries_the_60s_hint(mem):
    mem.prom_range = lambda query, window_s, step_s: []
    result = get_metric("rate(nope_total[5m])")
    assert result["series"] == [] and result["hint"] == NO_DATA_HINT
    assert "about every 60 s" in registry.get(result["evidence"])["summary"]
    assert "adjusted" not in result
