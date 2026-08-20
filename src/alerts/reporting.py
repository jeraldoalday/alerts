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
from datetime import datetime, timedelta
from typing import List, Optional, Sequence

from sqlalchemy.orm import Session

from alerts.config import MaintenanceWindowConfig
from alerts.maintenance import active_occurrence, describe_schedule, next_occurrence
from alerts.models import Incident, IncidentState, MonitorState
from alerts.timeutil import as_utc_naive, utcnow
from alerts.web import queries
from alerts.web.stats import compute_uptime_stats

_COLUMNS = ("MONITOR", "TYPE", "STATE", "UPTIME", "LATENCY", "LAST CHECK", "NOTE")
_MAINTENANCE_COLUMNS = ("WINDOW", "SCHEDULE", "MONITORS", "STATUS")
_INCIDENT_COLUMNS = ("MONITOR", "STARTED", "DURATION", "STATE", "CAUSE")

# A cause is a checker's error message, which can run to a whole TLS
# handshake failure. Truncated so one long message doesn't push the other
# columns off an 80-column terminal; the full text is on the dashboard.
_CAUSE_WIDTH = 44


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


@dataclass(frozen=True)
class MaintenanceRow:
    """One maintenance window's line in the `alerts maintenance` table."""

    name: str
    schedule: str
    monitors: List[str]
    active_until: Optional[datetime]
    next_start: Optional[datetime]

    @property
    def is_active(self) -> bool:
        return self.active_until is not None


def collect_maintenance_rows(
    windows: Sequence[MaintenanceWindowConfig],
    *,
    monitor: Optional[str] = None,
    now: Optional[datetime] = None,
) -> List[MaintenanceRow]:
    """Build one row per configured window.

    `monitor` narrows the report to the windows covering that monitor, which
    is the question actually being asked before starting a change: is this
    service covered right now?

    A window is asked whether it is active for a specific monitor, so an
    unfiltered report resolves "active" against the first monitor the window
    names -- a window scoped to a monitor is active for all of them or none,
    since the schedule doesn't vary per monitor.
    """
    moment = as_utc_naive(now or utcnow())
    rows: List[MaintenanceRow] = []
    for window in windows:
        if monitor is not None and not window.applies_to(monitor):
            continue
        subject = monitor or (window.monitors[0] if window.monitors else "*")
        active = active_occurrence(window, subject, moment)
        upcoming = next_occurrence(window, now=moment)
        rows.append(
            MaintenanceRow(
                name=window.name,
                schedule=describe_schedule(window),
                monitors=list(window.monitors),
                active_until=active.ends_at if active else None,
                next_start=upcoming.starts_at if upcoming else None,
            )
        )
    return rows


def _maintenance_status(row: MaintenanceRow, now: datetime) -> str:
    if row.active_until is not None:
        remaining = format_duration((row.active_until - as_utc_naive(now)).total_seconds())
        return f"ACTIVE, {remaining} left"
    if row.next_start is not None:
        until = format_duration((row.next_start - as_utc_naive(now)).total_seconds())
        return f"in {until}"
    return "past"


def _maintenance_cells(row: MaintenanceRow, now: datetime) -> List[str]:
    return [
        row.name,
        row.schedule,
        ",".join(row.monitors) if row.monitors else "all",
        _maintenance_status(row, now),
    ]


def format_maintenance_table(
    rows: Sequence[MaintenanceRow], *, now: Optional[datetime] = None
) -> str:
    """Render maintenance windows as an aligned plain-text table."""
    now = now or utcnow()
    if not rows:
        return "No maintenance windows configured."

    table = [list(_MAINTENANCE_COLUMNS)] + [_maintenance_cells(row, now) for row in rows]
    widths = [
        max(len(line[column]) for line in table) for column in range(len(_MAINTENANCE_COLUMNS))
    ]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(line, widths)).rstrip()
        for line in table
    )


def format_maintenance_summary(rows: Sequence[MaintenanceRow]) -> str:
    """One-line tally of how many windows are configured and active."""
    active = sum(1 for row in rows if row.is_active)
    return f"{len(rows)} window(s), {active} active"


@dataclass(frozen=True)
class IncidentRow:
    """One incident's line in the `alerts incidents` table."""

    monitor: str
    state: IncidentState
    started_at: datetime
    resolved_at: Optional[datetime]
    cause: Optional[str]

    @property
    def is_open(self) -> bool:
        return self.state == IncidentState.OPEN

    def duration_seconds(self, now: datetime) -> float:
        """How long the incident lasted, or has lasted so far.

        An open incident is measured against `now`, which is what makes the
        total downtime figure below keep climbing while something is broken
        rather than reporting only the outages that have already ended.
        """
        end = self.resolved_at if self.resolved_at is not None else now
        return (as_utc_naive(end) - as_utc_naive(self.started_at)).total_seconds()


def collect_incident_rows(
    session: Session,
    *,
    monitor: Optional[str] = None,
    since_days: Optional[int] = 7,
    open_only: bool = False,
    limit: int = 50,
    now: Optional[datetime] = None,
) -> List[IncidentRow]:
    """Build one row per incident, newest first.

    `monitor` names one service; an unknown name yields no rows rather than
    an error, matching how the dashboard treats a name nothing matches.
    `since_days=None` drops the time window and reports as far back as
    retention has kept.
    """
    moment = as_utc_naive(now or utcnow())
    monitor_id: Optional[int] = None
    if monitor is not None:
        found = queries.get_monitor_by_name(session, monitor)
        if found is None:
            return []
        monitor_id = found.id

    since = moment - timedelta(days=since_days) if since_days is not None else None
    names = {m.id: m.name for m in queries.list_monitors(session)}
    incidents = queries.list_incidents(
        session, monitor_id=monitor_id, since=since, open_only=open_only, limit=limit
    )
    return [
        IncidentRow(
            monitor=names.get(incident.monitor_id, f"#{incident.monitor_id}"),
            state=incident.state,
            started_at=incident.started_at,
            resolved_at=incident.resolved_at,
            cause=incident.cause,
        )
        for incident in incidents
    ]


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "\u2026"


def _incident_cells(row: IncidentRow, now: datetime) -> List[str]:
    duration = format_duration(row.duration_seconds(now))
    return [
        row.monitor,
        as_utc_naive(row.started_at).strftime("%Y-%m-%d %H:%M"),
        f"{duration} so far" if row.is_open else duration,
        row.state.value,
        _truncate(row.cause, _CAUSE_WIDTH) if row.cause else "",
    ]


def format_incident_table(
    rows: Sequence[IncidentRow], *, now: Optional[datetime] = None
) -> str:
    """Render incidents as an aligned plain-text table, newest first."""
    now = now or utcnow()
    if not rows:
        return "No incidents in the reporting window."

    table = [list(_INCIDENT_COLUMNS)] + [_incident_cells(row, now) for row in rows]
    widths = [
        max(len(line[column]) for line in table) for column in range(len(_INCIDENT_COLUMNS))
    ]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(line, widths)).rstrip()
        for line in table
    )


def format_incident_summary(
    rows: Sequence[IncidentRow], *, now: Optional[datetime] = None
) -> str:
    """One-line tally: how many incidents, how much downtime, and the mean recovery.

    Mean time to recovery covers only the resolved incidents, because an open
    one hasn't recovered and folding its running duration in would drag the
    average down every time it is recomputed -- an outage getting longer
    would appear to improve the number. Total downtime does include open
    incidents, since that time is being lost right now.

    The two figures are deliberately not derived from each other: MTTR is
    about how quickly things come back, total downtime is about how much
    service was lost, and one incident that has been open for a week says
    very different things about each.
    """
    now = now or utcnow()
    if not rows:
        return "0 incident(s)"

    open_count = sum(1 for row in rows if row.is_open)
    total_downtime = sum(row.duration_seconds(now) for row in rows)
    resolved = [row for row in rows if not row.is_open]

    parts = [f"{len(rows)} incident(s)"]
    if open_count:
        parts.append(f"{open_count} still open")
    parts.append(f"{format_duration(total_downtime)} total downtime")
    if resolved:
        mttr = sum(row.duration_seconds(now) for row in resolved) / len(resolved)
        parts.append(f"mean recovery {format_duration(mttr)}")
    return ", ".join(parts)


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
