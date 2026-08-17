from alerts.models import CheckResult
from alerts.web.stats import compute_uptime_stats


def _result(success: bool, latency_ms=None) -> CheckResult:
    return CheckResult(success=success, latency_ms=latency_ms)


def test_no_checks_yields_no_data():
    stats = compute_uptime_stats([])

    assert stats.total_checks == 0
    assert stats.successful_checks == 0
    assert stats.uptime_percentage is None
    assert stats.average_latency_ms is None


def test_all_successful_checks_is_100_percent_uptime():
    results = [_result(True, 10.0), _result(True, 20.0), _result(True, 30.0)]

    stats = compute_uptime_stats(results)

    assert stats.total_checks == 3
    assert stats.successful_checks == 3
    assert stats.uptime_percentage == 100.0
    assert stats.average_latency_ms == 20.0


def test_all_failed_checks_is_0_percent_uptime():
    results = [_result(False), _result(False)]

    stats = compute_uptime_stats(results)

    assert stats.successful_checks == 0
    assert stats.uptime_percentage == 0.0


def test_mixed_results_computes_percentage():
    results = [_result(True), _result(True), _result(True), _result(False)]

    stats = compute_uptime_stats(results)

    assert stats.total_checks == 4
    assert stats.successful_checks == 3
    assert stats.uptime_percentage == 75.0


def test_latency_average_ignores_checks_with_no_latency():
    results = [_result(True, 10.0), _result(False, None), _result(True, 30.0)]

    stats = compute_uptime_stats(results)

    assert stats.average_latency_ms == 20.0


def test_latency_average_is_none_when_no_result_has_latency():
    results = [_result(False, None), _result(False, None)]

    stats = compute_uptime_stats(results)

    assert stats.average_latency_ms is None
