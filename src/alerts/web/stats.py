"""Pure aggregation helpers over a monitor's check-result history.

Kept separate from the dashboard routes so the uptime/latency math can be
tested against plain `CheckResult` instances without a database.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence

from alerts.models import CheckResult, Monitor, MonitorState

# The quantiles reported alongside the mean. p50 is the typical request, p95
# is the one an unlucky user gets, p99 is the tail that a latency budget is
# usually written against.
LATENCY_PERCENTILES = (50, 95, 99)


def percentile(values: Sequence[float], percent: float) -> Optional[float]:
    """The nearest-rank percentile of a set of samples.

    Nearest-rank rather than an interpolating definition: every figure this
    returns is a latency some check actually recorded, so "p95 1624ms" names
    a real request that took that long instead of a weighted average of two
    that didn't. Over the small windows this dashboard aggregates -- 50
    checks by default -- interpolation mostly invents precision the sample
    size doesn't support.

    Returns None for an empty sample, matching how `UptimeStats` reports "no
    data" rather than a misleading zero.
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = math.ceil((percent / 100) * len(ordered))
    index = min(max(rank, 1), len(ordered)) - 1
    return ordered[index]


@dataclass(frozen=True)
class UptimeStats:
    """Aggregate stats over a window of check results.

    `uptime_percentage` and `average_latency_ms` are `None` rather than 0
    when there's nothing to average over, so callers can render "no data"
    instead of a misleading 0%. The percentile fields follow the same rule.

    The percentiles sit next to the mean rather than replacing it because
    they answer a different question. A mean of 90ms hides the one request in
    twenty that took four seconds; `p95_latency_ms` is what a user actually
    complains about, and it is the figure a `degraded_latency_ms` budget
    should be set against.
    """

    total_checks: int
    successful_checks: int
    uptime_percentage: Optional[float]
    average_latency_ms: Optional[float]
    p50_latency_ms: Optional[float] = None
    p95_latency_ms: Optional[float] = None
    p99_latency_ms: Optional[float] = None

    @property
    def latency_percentiles(self) -> Dict[int, Optional[float]]:
        """The percentile fields keyed by percentile, for uniform rendering.

        Every caller that shows them shows all of them in the same order, so
        the mapping keeps the three field names out of the templates, the
        JSON payload, and the metrics exposition.
        """
        return {
            50: self.p50_latency_ms,
            95: self.p95_latency_ms,
            99: self.p99_latency_ms,
        }


def compute_uptime_stats(check_results: Iterable[CheckResult]) -> UptimeStats:
    results: List[CheckResult] = list(check_results)
    total = len(results)
    if total == 0:
        return UptimeStats(
            total_checks=0,
            successful_checks=0,
            uptime_percentage=None,
            average_latency_ms=None,
        )

    successful = sum(1 for result in results if result.success)
    latencies = [result.latency_ms for result in results if result.latency_ms is not None]
    average_latency = sum(latencies) / len(latencies) if latencies else None
    percentiles = {p: percentile(latencies, p) for p in LATENCY_PERCENTILES}

    return UptimeStats(
        total_checks=total,
        successful_checks=successful,
        uptime_percentage=(successful / total) * 100,
        average_latency_ms=average_latency,
        p50_latency_ms=percentiles[50],
        p95_latency_ms=percentiles[95],
        p99_latency_ms=percentiles[99],
    )


@dataclass(frozen=True)
class StateSummary:
    """How many monitors sit in each state right now.

    Every `MonitorState` is present in `counts`, zeroes included, so callers
    can render a fixed set of tiles without deciding what to do about a state
    nothing happens to be in.

    Paused monitors -- ones the config no longer declares -- are counted on
    their own and left out of `total` and `counts`. Their last known state is
    frozen at whatever it was when checking stopped, so folding a monitor
    nobody watches any more into the "down" tile would report an outage that
    isn't being observed, let alone happening.
    """

    total: int
    counts: Dict[MonitorState, int]
    open_incidents: int = 0
    paused: int = 0

    @property
    def all_up(self) -> bool:
        return self.total > 0 and self.counts[MonitorState.UP] == self.total


def summarize_states(monitors: Iterable[Monitor], open_incidents: int = 0) -> StateSummary:
    counts = {state: 0 for state in MonitorState}
    total = 0
    paused = 0
    for monitor in monitors:
        if monitor.is_paused:
            paused += 1
            continue
        counts[monitor.state] += 1
        total += 1
    return StateSummary(
        total=total, counts=counts, open_incidents=open_incidents, paused=paused
    )
