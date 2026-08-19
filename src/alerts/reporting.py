"""Plain-text status reporting for `alerts status`.

The dashboard already answers "what's the state of everything right now",
but it needs a running process and a browser. This renders the same picture
to stdout so it works over SSH, in a cron mail, or as a scripted check.

Table rendering is done by hand rather than pulled in from a dependency: the
project's dependency list is deliberately short, and column alignment over a
handful of rows is not worth adding to it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Sequence

from sqlalchemy.orm import Session

from alerts.models import Incident, IncidentState, MonitorState
from alerts.timeutil import as_utc_naive, utcnow
from alerts.web import queries
from alerts.web.stats import compute_uptime_stats

_COLUMNS = ("MONITOR", "TYPE", "STATE", "UPTIME", "LATENCY", "LAST CHECK", "NOTE")


@dataclass(frozen=True)
class StatusRow:
    """One monitor's line in the status table."""

    name: str
    type: str
    state: MonitorState
    uptime_percentage: Optional[float]
    average_latency_ms: Optional[float]
    last_checked_at: Optional[datetime]
    open_incident_since: Optional[datetime]
    paused: bool = False

    @property
    def is_down(self) -> bool:
        """Whether this row should fail `alerts status --exit-code`.

        A paused monitor is excluded however bad its last known state was:
        the config stopped declaring it, nothing is checking it any more, and
        failing a scripted check on a frozen state would keep an operator
        chasing an outage that is no longer being observed.
        """
        return self.state == MonitorState.DOWN and not self.paused


def _open_incident(session: Session, monitor_id: int) -> Optional[Incident]:
    return (
        session.query(Incident)
        .filter_by(monitor_id=monitor_id, state=IncidentState.OPEN)
        .order_by(Incident.started_at.desc())
        .first()
    )


def collect_status_rows(session: Session, *, checks_window: int = 50) -> List[StatusRow]:
    """Build one row per monitor, with uptime over its last `checks_window` checks."""
    rows: List[StatusRow] = []
    for monitor in queries.list_monitors(session):
        checks = queries.recent_check_results(session, monitor.id, checks_window)
        stats = compute_uptime_stats(checks)
        incident = _open_incident(session, monitor.id)
        rows.append(
            StatusRow(
                name=monitor.name,
                type=monitor.type.value,
                state=monitor.state,
                uptime_percentage=stats.uptime_percentage,
                average_latency_ms=stats.average_latency_ms,
                last_checked_at=checks[0].checked_at if checks else None,
                open_incident_since=incident.started_at if incident else None,
                paused=monitor.is_paused,
            )
        )
    return rows


def format_duration(seconds: float) -> str:
    """A compact, human-readable duration ("3d 4h", "12m")."""
    seconds = max(int(seconds), 0)
    days, seconds = divmod(seconds, 86_400)
    hours, seconds = divmod(seconds, 3_600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


def _relative_age(value: Optional[datetime], now: datetime) -> str:
    if value is None:
        return "never"
    return f"{format_duration((as_utc_naive(now) - as_utc_naive(value)).total_seconds())} ago"


def _note(row: StatusRow, now: datetime) -> str:
    if row.paused:
        return "paused, not in config"
    if row.open_incident_since is None:
        return ""
    down_for = format_duration(
        (as_utc_naive(now) - as_utc_naive(row.open_incident_since)).total_seconds()
    )
    return f"open incident, {down_for}"


def _cells(row: StatusRow, now: datetime) -> List[str]:
    uptime = "-" if row.uptime_percentage is None else f"{row.uptime_percentage:.1f}%"
    latency = "-" if row.average_latency_ms is None else f"{row.average_latency_ms:.0f}ms"
    return [
        row.name,
        row.type,
        row.state.value,
        uptime,
        latency,
        _relative_age(row.last_checked_at, now),
        _note(row, now),
    ]


def format_status_table(rows: Sequence[StatusRow], *, now: Optional[datetime] = None) -> str:
    """Render rows as an aligned plain-text table.

    Trailing whitespace is stripped from every line, so an empty NOTE column
    doesn't leave the table padded out to a width nothing is using.
    """
    now = now or utcnow()
    if not rows:
        return "No monitors configured."

    table = [list(_COLUMNS)] + [_cells(row, now) for row in rows]
    widths = [max(len(line[column]) for line in table) for column in range(len(_COLUMNS))]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(line, widths)).rstrip()
        for line in table
    )


def format_status_summary(rows: Sequence[StatusRow]) -> str:
    """One-line tally of how many monitors sit in each state.

    Paused monitors are tallied on their own rather than folded into their
    last known state, for the same reason they don't fail `--exit-code`: that
    state is a frozen reading, not a current one.
    """
    counts = {state: 0 for state in MonitorState}
    paused = 0
    for row in rows:
        if row.paused:
            paused += 1
            continue
        counts[row.state] += 1
    parts = [f"{counts[state]} {state.value}" for state in MonitorState if counts[state]]
    if paused:
        parts.append(f"{paused} paused")
    tally = ", ".join(parts)
    return f"{len(rows)} monitor(s): {tally}" if tally else f"{len(rows)} monitor(s)"
