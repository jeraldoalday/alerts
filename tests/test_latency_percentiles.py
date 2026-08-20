"""Latency percentiles over a window of check results.

The mean is an average of everything; a percentile is a promise about a
proportion. These pin the nearest-rank definition `stats.percentile` uses,
because the interesting disagreements between percentile definitions all
happen at the small sample sizes this dashboard actually aggregates.
"""
import pytest

from alerts.models import CheckResult
from alerts.web.stats import LATENCY_PERCENTILES, compute_uptime_stats, percentile


def _result(success: bool = True, latency_ms=None) -> CheckResult:
    return CheckResult(success=success, latency_ms=latency_ms)


def _results(*latencies) -> list:
    return [_result(True, latency) for latency in latencies]


def test_an_empty_sample_has_no_percentile():
    assert percentile([], 95) is None


def test_a_single_sample_is_every_percentile():
    assert percentile([12.0], 50) == 12.0
    assert percentile([12.0], 95) == 12.0
    assert percentile([12.0], 99) == 12.0


def test_the_median_of_an_odd_sample_is_the_middle_value():
    assert percentile([30.0, 10.0, 20.0], 50) == 20.0


def test_the_median_of_an_even_sample_takes_the_lower_middle():
    """Nearest-rank, not an average of the two middle samples.

    ceil(0.5 * 4) == 2, so the second of four ordered samples wins. An
    interpolating median would answer 25.0, which no check ever recorded.
    """
    assert percentile([10.0, 20.0, 30.0, 40.0], 50) == 20.0


def test_the_input_order_does_not_matter():
    ascending = percentile([1.0, 2.0, 3.0, 4.0, 5.0], 95)
    descending = percentile([5.0, 4.0, 3.0, 2.0, 1.0], 95)

    assert ascending == descending == 5.0


def test_the_caller_s_list_is_not_reordered():
    """Sorting in place would shuffle the caller's samples underneath it."""
    latencies = [30.0, 10.0, 20.0]

    percentile(latencies, 95)

    assert latencies == [30.0, 10.0, 20.0]


def test_p100_is_the_slowest_sample():
    assert percentile([10.0, 90.0, 50.0], 100) == 90.0


def test_p0_is_the_fastest_sample():
    """Rank 0 is clamped up to the first sample rather than indexing off the end."""
    assert percentile([10.0, 90.0, 50.0], 0) == 10.0


def test_p95_of_twenty_samples_is_the_nineteenth():
    latencies = [float(n) for n in range(1, 21)]

    assert percentile(latencies, 95) == 19.0


def test_a_single_outlier_moves_the_tail_but_not_the_median():
    """The whole reason percentiles sit next to the mean.

    Nineteen fast checks and one four-second one average to 285ms, which
    reads as a healthy service. p95 names the four-second check.
    """
    latencies = [90.0] * 19 + [4000.0]

    stats = compute_uptime_stats(_results(*latencies))

    assert stats.average_latency_ms == pytest.approx(285.5)
    assert stats.p50_latency_ms == 90.0
    assert stats.p95_latency_ms == 90.0
    assert stats.p99_latency_ms == 4000.0


def test_compute_uptime_stats_reports_all_three_percentiles():
    stats = compute_uptime_stats(_results(*[float(n) for n in range(1, 101)]))

    assert stats.p50_latency_ms == 50.0
    assert stats.p95_latency_ms == 95.0
    assert stats.p99_latency_ms == 99.0


def test_percentiles_ignore_checks_that_recorded_no_latency():
    """A failed connection has no latency to rank, only an absence of one.

    Treating a missing latency as 0 would drag every percentile down and make
    an outage look like the fastest the service has ever been.
    """
    results = [_result(True, 100.0), _result(False, None), _result(True, 300.0)]

    stats = compute_uptime_stats(results)

    assert stats.p50_latency_ms == 100.0
    assert stats.p99_latency_ms == 300.0


def test_percentiles_are_none_when_nothing_recorded_a_latency():
    stats = compute_uptime_stats([_result(False), _result(False)])

    assert stats.p50_latency_ms is None
    assert stats.p95_latency_ms is None
    assert stats.p99_latency_ms is None


def test_no_checks_at_all_has_no_percentiles():
    stats = compute_uptime_stats([])

    assert stats.latency_percentiles == {50: None, 95: None, 99: None}


def test_the_percentile_mapping_matches_the_fields():
    stats = compute_uptime_stats(_results(10.0, 20.0, 30.0, 40.0, 50.0))

    assert stats.latency_percentiles == {
        50: stats.p50_latency_ms,
        95: stats.p95_latency_ms,
        99: stats.p99_latency_ms,
    }


def test_the_mapping_covers_exactly_the_declared_percentiles():
    stats = compute_uptime_stats(_results(10.0))

    assert tuple(stats.latency_percentiles) == LATENCY_PERCENTILES


def test_percentiles_never_decrease_as_the_percentile_rises():
    stats = compute_uptime_stats(_results(*[float(n) for n in range(1, 61)]))

    assert stats.p50_latency_ms <= stats.p95_latency_ms <= stats.p99_latency_ms
