"""Pure aggregation helpers over a monitor's check-result history.

Kept separate from the dashboard routes so the uptime/latency math can be
tested against plain `CheckResult` instances without a database.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

from alerts.models import CheckResult, Monitor, MonitorState


@dataclass(frozen=True)
class UptimeStats:
    """Aggregate stats over a window of check results.

    `uptime_percentage` and `average_latency_ms` are `None` rather than 0
    when there's nothing to average over, so callers can render "no data"
    instead of a misleading 0%.
    """

    total_checks: int
    successful_checks: int
    uptime_percentage: Optional[float]
    average_latency_ms: Optional[float]


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

    return UptimeStats(
        total_checks=total,
        successful_checks=successful,
        uptime_percentage=(successful / total) * 100,
        average_latency_ms=average_latency,
    )


@dataclass(frozen=True)
class StateSummary:
    """How many monitors sit in each state right now.

    Every `MonitorState` is present in `counts`, zeroes included, so callers
    can render a fixed set of tiles without deciding what to do about a state
    nothing happens to be in.
    """

    total: int
    counts: Dict[MonitorState, int]
    open_incidents: int = 0

    @property
    def all_up(self) -> bool:
        return self.total > 0 and self.counts[MonitorState.UP] == self.total


def summarize_states(monitors: Iterable[Monitor], open_incidents: int = 0) -> StateSummary:
    counts = {state: 0 for state in MonitorState}
    total = 0
    for monitor in monitors:
        counts[monitor.state] += 1
        total += 1
    return StateSummary(total=total, counts=counts, open_incidents=open_incidents)
