"""Plain-text tables for the reporting subcommands.

One module per table would be six modules that all render aligned columns
the same way, so they live together: `alerts status` (what is the state of
everything now), `alerts incidents` (what has been breaking), `alerts
maintenance` (what is planned), `alerts check` (what does this config
actually do right now), `alerts slo` (what has been promised, and how much
of the budget is left), and `alerts deps` (what needs what, and who is
being kept quiet because of it).

The dashboard answers the first of those too, but it needs a running process
and a browser. These render the same pictures to stdout so they work over
SSH, in a cron mail, or as a scripted check.

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
from alerts.dependencies import describe_chain, is_held_back
from alerts.maintenance import active_occurrence, describe_schedule, next_occurrence
from alerts.models import Incident, IncidentState, MonitorState
from alerts.probe import ProbeResult
from alerts.slo import BudgetStatus, ErrorBudget
from alerts.timeutil import as_utc_naive, format_duration, utcnow
from alerts.web import queries
from alerts.web.stats import compute_uptime_stats

_COLUMNS = (
    "MONITOR",
    "TYPE",
    "TAGS",
    "STATE",
    "UPTIME",
    "LATENCY",
    "P95",
    "LAST CHECK",
    "NOTE",
)
_MAINTENANCE_COLUMNS = ("WINDOW", "SCHEDULE", "MONITORS", "STATUS")
_INCIDENT_COLUMNS = ("MONITOR", "STARTED", "DURATION", "STATE", "CAUSE")
_PROBE_COLUMNS = ("MONITOR", "TYPE", "TARGET", "RESULT", "LATENCY", "DETAIL")
_DEPENDENCY_COLUMNS = ("MONITOR", "STATE", "DEPENDS ON", "UPSTREAM DOWN", "NOTE")
_SLO_COLUMNS = (
    "MONITOR",
    "TAGS",
    "TARGET",
    "WINDOW",
    "ACTUAL",
    "CHECKS",
    "BUDGET",
    "STATUS",
    "NOTE",
)

# `alerts status` shows a monitor's tags so an unfiltered table can still be
# read by group. The probe table doesn't: it is already showing the target,
# and a run is usually narrowed to a group rather than scanned for one.

# A cause is a checker's error message, which can run to a whole TLS
# handshake failure. Truncated so one long message doesn't push the other
# columns off an 80-column terminal; the full text is on the dashboard.
_CAUSE_WIDTH = 44

# A probe's target is a whole URL and its detail is a checker message, so
# both get their own budget. Wider than _CAUSE_WIDTH because `alerts check`
# is run interactively, where a wrapped line costs less than a truncated URL
# that can't be told apart from a different one with the same prefix.
_TARGET_WIDTH = 48
_DETAIL_WIDTH = 60


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
    # Defaulted rather than required so a row built by hand -- in a test, or
    # by anything assembling this table from somewhere other than
    # `collect_status_rows` -- doesn't have to invent a percentile.
    p95_latency_ms: Optional[float] = None
    tags: Sequence[str] = ()
    # The monitors this one depends on that are down right now. Carried so
    # the NOTE column can explain a silence: a row reading `down` with
    # nobody paged about it looks like the notifier is broken, and this is
    # the difference between that and a dependency doing its job.
    blocked_by: Sequence[str] = ()

    @property
    def alerts_held_back(self) -> bool:
        """Whether this monitor is down and being kept quiet by an upstream."""
        return is_held_back(
            is_down=self.state == MonitorState.DOWN,
            is_paused=self.paused,
            blocked_by=self.blocked_by,
        )

    @property
    def is_down(self) -> bool:
        """Whether this row should fail `alerts status --exit-code`.

        A paused monitor is excluded however bad its last known state was:
        the config stopped declaring it, nothing is checking it any more, and
        failing a scripted check on a frozen state would keep an operator
        chasing an outage that is no longer being observed.

        A monitor whose alerts are held back is *not* excluded. Nobody was
        paged about it, but it is down, and a script asking "is anything
        broken" is asking about the estate rather than about who got told.
        """
        return self.state == MonitorState.DOWN and not self.paused


def _open_incident(session: Session, monitor_id: int) -> Optional[Incident]:
    return (
        session.query(Incident)
        .filter_by(monitor_id=monitor_id, state=IncidentState.OPEN)
        .order_by(Incident.started_at.desc())
        .first()
    )


def collect_status_rows(
    session: Session,
    *,
    checks_window: int = 50,
    monitor: Optional[str] = None,
    tag: Optional[str] = None,
) -> List[StatusRow]:
    """Build one row per monitor, with uptime over its last `checks_window` checks.

    `monitor` and `tag` narrow the report, and narrow together rather than
    override, the same as every other report in this module. A filter
    nothing matches yields no rows rather than an error: the summary line
    underneath says "0 monitor(s)", which is the honest answer to a
    question about a group that is empty.

    Down upstreams are resolved against the whole estate rather than the
    filtered set: a `--tag payments` table still has to be able to explain
    that its checkout page is quiet because of a database nobody on that
    team tagged.
    """
    found_monitors = queries.list_monitors(session, tag=tag)
    if monitor is not None:
        found_monitors = [found for found in found_monitors if found.name == monitor]
    blocked = queries.blocking_upstreams(session, found_monitors)
    rows: List[StatusRow] = []
    for monitor_row in found_monitors:
        checks = queries.recent_check_results(session, monitor_row.id, checks_window)
        stats = compute_uptime_stats(checks)
        incident = _open_incident(session, monitor_row.id)
        rows.append(
            StatusRow(
                name=monitor_row.name,
                type=monitor_row.type.value,
                state=monitor_row.state,
                uptime_percentage=stats.uptime_percentage,
                average_latency_ms=stats.average_latency_ms,
                p95_latency_ms=stats.p95_latency_ms,
                last_checked_at=checks[0].checked_at if checks else None,
                open_incident_since=incident.started_at if incident else None,
                paused=monitor_row.is_paused,
                tags=tuple(monitor_row.tags or ()),
                blocked_by=tuple(blocked.get(monitor_row.name, ())),
            )
        )
    return rows


def _relative_age(value: Optional[datetime], now: datetime) -> str:
    if value is None:
        return "never"
    return f"{format_duration((as_utc_naive(now) - as_utc_naive(value)).total_seconds())} ago"


def _note(row: StatusRow, now: datetime) -> str:
    """The NOTE cell: how long this has been broken, and why it is quiet.

    The suppression is appended to the open-incident note rather than
    replacing it, because they answer different questions and an operator
    looking at this table during an outage has both: how long has this been
    down, and why has nobody been paged about it. A row reading `down` with
    silence around it otherwise looks like the notifier has failed.
    """
    if row.paused:
        return "paused, not in config"
    parts = []
    if row.open_incident_since is not None:
        down_for = format_duration(
            (as_utc_naive(now) - as_utc_naive(row.open_incident_since)).total_seconds()
        )
        parts.append(f"open incident, {down_for}")
    if row.alerts_held_back:
        parts.append(f"alerts held back by {', '.join(row.blocked_by)}")
    return "; ".join(parts)


def _latency(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:.0f}ms"


def _cells(row: StatusRow, now: datetime) -> List[str]:
    uptime = "-" if row.uptime_percentage is None else f"{row.uptime_percentage:.1f}%"
    return [
        row.name,
        row.type,
        ",".join(row.tags),
        row.state.value,
        uptime,
        _latency(row.average_latency_ms),
        _latency(row.p95_latency_ms),
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
    tag: Optional[str] = None,
    since_days: Optional[int] = 7,
    open_only: bool = False,
    limit: int = 50,
    now: Optional[datetime] = None,
) -> List[IncidentRow]:
    """Build one row per incident, newest first.

    `monitor` names one service; an unknown name yields no rows rather than
    an error, matching how the dashboard treats a name nothing matches.
    `tag` narrows to every monitor carrying it, the same way `--tag` narrows
    `alerts status`; both narrow together when given at once, so `--monitor
    api --tag prod` reports `api` only if it is tagged `prod`. A tag nothing
    carries is treated the same as an unknown monitor name: no rows, not an
    error.
    `since_days=None` drops the time window and reports as far back as
    retention has kept. An incident that is still open is reported whatever
    the window says -- see `queries.list_incidents`.
    """
    moment = as_utc_naive(now or utcnow())
    monitor_id: Optional[int] = None
    if monitor is not None:
        found = queries.get_monitor_by_name(session, monitor)
        if found is None:
            return []
        monitor_id = found.id

    monitor_ids: Optional[List[int]] = None
    if tag is not None:
        tagged_ids = {m.id for m in queries.list_monitors(session, tag=tag)}
        if monitor_id is not None:
            if monitor_id not in tagged_ids:
                return []
        elif not tagged_ids:
            return []
        else:
            monitor_ids = sorted(tagged_ids)

    since = moment - timedelta(days=since_days) if since_days is not None else None
    names = {m.id: m.name for m in queries.list_monitors(session)}
    incidents = queries.list_incidents(
        session,
        monitor_id=monitor_id,
        monitor_ids=monitor_ids,
        since=since,
        open_only=open_only,
        limit=limit,
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


# The three outcomes a probe reports, spelled so they sort visually: a
# failure is the only one in capitals, because it is the only one anyone
# scanning a long table needs to find.
_PROBE_OK = "ok"
_PROBE_SLOW = "slow"
_PROBE_FAIL = "FAIL"


@dataclass(frozen=True)
class ProbeRow:
    """One monitor's line in the `alerts check` table."""

    name: str
    type: str
    target: str
    result: str
    latency_ms: Optional[float]
    detail: str

    @property
    def failed(self) -> bool:
        return self.result == _PROBE_FAIL


def collect_probe_rows(results: Sequence[ProbeResult]) -> List[ProbeRow]:
    """Build one row per probed monitor, in the order they were probed."""
    rows: List[ProbeRow] = []
    for result in results:
        if not result.passed:
            outcome = _PROBE_FAIL
        elif result.is_slow:
            outcome = _PROBE_SLOW
        else:
            outcome = _PROBE_OK
        rows.append(
            ProbeRow(
                name=result.name,
                type=result.type,
                target=result.target,
                result=outcome,
                latency_ms=result.outcome.latency_ms,
                detail=result.detail,
            )
        )
    return rows


def _probe_cells(row: ProbeRow) -> List[str]:
    return [
        row.name,
        row.type,
        _truncate(row.target, _TARGET_WIDTH),
        row.result,
        _latency(row.latency_ms),
        _truncate(row.detail, _DETAIL_WIDTH),
    ]


def format_probe_table(rows: Sequence[ProbeRow]) -> str:
    """Render probe results as an aligned plain-text table.

    The target is shown, unlike in the status table, because this command is
    usually run against a config being edited: knowing that `api-gateway`
    failed matters much less than seeing which URL it actually asked.
    """
    if not rows:
        return "No monitors to check."

    table = [list(_PROBE_COLUMNS)] + [_probe_cells(row) for row in rows]
    widths = [max(len(line[column]) for line in table) for column in range(len(_PROBE_COLUMNS))]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(line, widths)).rstrip()
        for line in table
    )


def format_probe_summary(rows: Sequence[ProbeRow]) -> str:
    """One-line tally of how a probe run went.

    Slow checks are counted on their own rather than folded into either
    neighbour. They passed, so calling them failures would be wrong, but a
    run where everything came back a millisecond under its budget is not the
    clean bill of health a bare "3 ok" claims.

    The three counts partition the row set, so they add up to the total. A
    reader who has to work out whether "2 ok, 1 slow" out of 3 means one of
    the two was also the slow one is being given arithmetic, not a summary.
    """
    if not rows:
        return "0 monitor(s) checked"

    failed = sum(1 for row in rows if row.failed)
    slow = sum(1 for row in rows if row.result == _PROBE_SLOW)
    parts = [f"{len(rows)} monitor(s) checked", f"{len(rows) - failed - slow} ok"]
    if slow:
        parts.append(f"{slow} slow")
    parts.append(f"{failed} failed")
    return ", ".join(parts)


# How each budget status is spelled in the table. The two that need acting
# on are in capitals, following the probe table's convention: the point of a
# status column on a long report is that the rows worth reading can be found
# without reading the rest.
_SLO_STATUS_WORDS = {
    BudgetStatus.OK: "ok",
    BudgetStatus.AT_RISK: "AT RISK",
    BudgetStatus.BREACHED: "BREACHED",
    BudgetStatus.NO_DATA: "no data",
}


@dataclass(frozen=True)
class SloRow:
    """One monitor's line in the `alerts slo` table."""

    name: str
    budget: ErrorBudget
    tags: Sequence[str] = ()
    paused: bool = False

    @property
    def status(self) -> BudgetStatus:
        return self.budget.status

    @property
    def is_breaching(self) -> bool:
        """Whether this row should fail `alerts slo --exit-code`.

        A paused monitor is excluded however bad its figures look, matching
        `StatusRow.is_down`. Nothing is checking it, so its window is
        sliding forward over a history that stopped growing: the measured
        availability decays toward "no data" whatever the service is
        actually doing, and failing a scripted check on that would send
        someone after an outage nobody is observing.
        """
        return self.budget.is_breaching and not self.paused


def collect_slo_rows(
    session: Session,
    *,
    monitor: Optional[str] = None,
    tag: Optional[str] = None,
    now: Optional[datetime] = None,
) -> List[SloRow]:
    """Build one row per monitor carrying an availability objective.

    Monitors without a target are left out entirely rather than listed with
    empty columns. `alerts status` already lists every monitor; this report
    answers a narrower question, and padding it with rows that have nothing
    to say about that question makes the ones that do harder to find.

    `monitor` and `tag` narrow it the way they do everywhere else, and both
    narrow rather than override.
    """
    moment = now or utcnow()
    rows: List[SloRow] = []
    for found in queries.list_monitors(session, tag=tag):
        if monitor is not None and found.name != monitor:
            continue
        budget = queries.error_budget(session, found, now=moment)
        if budget is None:
            continue
        rows.append(
            SloRow(
                name=found.name,
                budget=budget,
                tags=tuple(found.tags or ()),
                paused=found.is_paused,
            )
        )
    return rows


def _percentage(value: Optional[float], places: int = 2) -> str:
    """A percentage at SLO precision, or a dash for nothing measured.

    Two decimal places rather than the status table's one: the whole
    vocabulary of availability targets lives in the decimals, and a report
    rounding 99.94% and 99.95% to the same "99.9%" cannot show a 99.95%
    target being missed.
    """
    return "-" if value is None else f"{value:.{places}f}%"


def _budget_cell(budget: ErrorBudget) -> str:
    """How much of the budget is left, as a percentage of the allowance.

    "spent" rather than "0% left" once it has been overrun, because those
    are different situations: one is a budget about to be exceeded and the
    other is one that already has been, and the STATUS column beside it is
    the place that says which.
    """
    remaining = budget.remaining_ratio
    if remaining is None:
        return "-"
    if remaining <= 0:
        return "spent"
    return f"{remaining * 100:.0f}% left"


def _slo_note(row: SloRow) -> str:
    """The budget in minutes, which is the unit an SLO gets discussed in.

    The columns to the left are the measurement -- checks, and the ratio
    between them. This is the translation: "18m of 43m left" is the sentence
    someone repeats in a planning meeting, and asking them to derive it from
    a check count and an interval they would have to go and look up is how a
    report goes unread.
    """
    if row.paused:
        return "paused, not in config"
    remaining = row.budget.remaining_downtime_seconds
    if remaining is None:
        return ""
    allowed = format_duration(row.budget.allowed_downtime_seconds)
    return f"{format_duration(remaining)} of {allowed} left"


def _slo_cells(row: SloRow) -> List[str]:
    budget = row.budget
    return [
        row.name,
        ",".join(row.tags),
        _percentage(budget.target_percentage),
        f"{budget.window_days}d",
        _percentage(budget.actual_percentage),
        str(budget.total_checks),
        _budget_cell(budget),
        _SLO_STATUS_WORDS[row.status],
        _slo_note(row),
    ]


def format_slo_table(rows: Sequence[SloRow]) -> str:
    """Render error budgets as an aligned plain-text table."""
    if not rows:
        return "No monitors have an SLO target."

    table = [list(_SLO_COLUMNS)] + [_slo_cells(row) for row in rows]
    widths = [max(len(line[column]) for line in table) for column in range(len(_SLO_COLUMNS))]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(line, widths)).rstrip()
        for line in table
    )


def format_slo_summary(rows: Sequence[SloRow]) -> str:
    """One-line tally of how the estate stands against what it promised.

    Paused monitors are counted on their own and left out of the status
    tally, for the same reason they don't fail `--exit-code`: their window
    is measuring a history that stopped growing.
    """
    if not rows:
        return "0 monitor(s) with an objective"

    counts = {status: 0 for status in BudgetStatus}
    paused = 0
    for row in rows:
        if row.paused:
            paused += 1
            continue
        counts[row.status] += 1
    parts = [
        f"{counts[status]} {_SLO_STATUS_WORDS[status].lower()}"
        for status in BudgetStatus
        if counts[status]
    ]
    if paused:
        parts.append(f"{paused} paused")
    tally = ", ".join(parts)
    return (
        f"{len(rows)} monitor(s) with an objective: {tally}"
        if tally
        else f"{len(rows)} monitor(s) with an objective"
    )


@dataclass(frozen=True)
class DependencyRow:
    """One monitor's line in the `alerts deps` table.

    `depends_on` is what the monitor declared, in the order it declared it.
    `blocked_by` is which of those is down right now, transitively: the
    reason a service is quiet may be two hops away, and the row exists to
    answer "why did nobody hear about this" without a second lookup.

    A down upstream is not by itself a suppressed alert. A healthy monitor
    sitting above a broken database has nothing to page about, so its row
    reports the down upstream and says nothing about alerts -- see
    `alerts_held_back`, which is the state that actually keeps somebody
    from being told.

    `dependents_held_back` is the same question from the other end -- how
    many services are failing right now and being kept quiet because of
    this one -- which is what turns the row for a broken database from a
    line in a table into the line worth reading first.
    """

    name: str
    state: MonitorState
    depends_on: Sequence[str] = ()
    blocked_by: Sequence[str] = ()
    dependents_held_back: int = 0
    paused: bool = False

    @property
    def is_blocked(self) -> bool:
        """Whether anything this monitor depends on is down right now."""
        return bool(self.blocked_by)

    @property
    def alerts_held_back(self) -> bool:
        """Whether this monitor is failing and being kept quiet because of it.

        The distinction the report lives or dies on -- see
        `dependencies.is_held_back`, which is where the rule lives.
        """
        return is_held_back(
            is_down=self.state == MonitorState.DOWN,
            is_paused=self.paused,
            blocked_by=self.blocked_by,
        )

    @property
    def is_root_cause(self) -> bool:
        """Whether this monitor is the reason other monitors are quiet.

        Down, not itself blocked by anything, and with something downstream
        of it whose alerts are being held back. On an estate where a
        database took an API and a checkout page down with it, this is the
        one row that names the thing to go and fix.
        """
        return (
            self.state == MonitorState.DOWN
            and not self.is_blocked
            and not self.paused
            and self.dependents_held_back > 0
        )


def collect_dependency_rows(
    session: Session, *, monitor: Optional[str] = None
) -> List[DependencyRow]:
    """Build one row per monitor involved in the dependency graph.

    Built from the monitors table rather than from the config, like every
    other report that has a state to show: the config says what should
    depend on what, and only the database knows what is down.

    Monitors that neither declare a dependency nor are one are left out
    entirely. An estate of forty services with three edges between them
    should print three or four rows, not forty mostly-empty ones, and the
    empty-table message says as much.

    Rows come out upstreams-first, so the chain reads down the page in the
    direction the failure travels.

    `monitor` narrows the report to that monitor's own neighbourhood --
    itself, everything it depends on, and everything that depends on it --
    because the question being asked of one monitor is either "why is this
    quiet" or "what else does this take with it", and both are answered by
    the same set.
    """
    monitors = {m.name: m for m in queries.list_monitors(session)}
    graph = queries.dependency_graph(session)
    down = {name for name, row in monitors.items() if row.blocks_dependents}
    blocked_by = {name: graph.blocking(name, down) for name in graph.names}
    # The dependents whose alerts are actually being suppressed: failing,
    # live, and with something down underneath them. A healthy monitor over
    # a broken database is not one of them, and counting it would have the
    # database's row claiming to be silencing pages nobody wants to send.
    held_back = {
        name
        for name, upstreams in blocked_by.items()
        if upstreams and name in monitors and monitors[name].blocks_dependents
    }

    involved = [name for name in graph.order() if graph.direct(name) or graph.dependents(name)]
    if monitor is not None:
        neighbourhood = {monitor, *graph.upstream(monitor), *graph.downstream(monitor)}
        involved = [name for name in involved if name in neighbourhood]

    rows: List[DependencyRow] = []
    for name in involved:
        row = monitors.get(name)
        if row is None:
            continue
        rows.append(
            DependencyRow(
                name=name,
                state=row.state,
                depends_on=graph.direct(name),
                blocked_by=blocked_by[name],
                dependents_held_back=sum(
                    1
                    for dependent in graph.downstream(name)
                    if dependent in held_back and name in blocked_by[dependent]
                ),
                paused=row.is_paused,
            )
        )
    return rows


def _dependency_note(row: DependencyRow) -> str:
    """The one thing worth saying about a dependency row.

    Ordered by what an operator does about it. A paused monitor is not
    participating at all, so nothing else on the row means anything. A
    blocked one explains a silence. A root cause is the row to act on, and
    says how much else is waiting on it.
    """
    if row.paused:
        return "paused, not in config"
    if row.alerts_held_back:
        return "alerts held back"
    if row.is_root_cause:
        return f"root cause, {row.dependents_held_back} dependent(s) held back"
    return ""


def _dependency_cells(row: DependencyRow) -> List[str]:
    return [
        row.name,
        row.state.value,
        describe_chain(list(row.depends_on)),
        describe_chain(list(row.blocked_by)),
        _dependency_note(row),
    ]


def format_dependency_table(rows: Sequence[DependencyRow]) -> str:
    """Render the dependency graph as an aligned plain-text table."""
    if not rows:
        return "No monitor declares a dependency."

    table = [list(_DEPENDENCY_COLUMNS)] + [_dependency_cells(row) for row in rows]
    widths = [
        max(len(line[column]) for line in table) for column in range(len(_DEPENDENCY_COLUMNS))
    ]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(line, widths)).rstrip()
        for line in table
    )


def format_dependency_summary(rows: Sequence[DependencyRow]) -> str:
    """One-line tally of the graph, and of what it is currently suppressing.

    The blocked count is the number worth printing even when it is zero:
    "nothing is being held back" is the reassurance someone reading this
    report during an incident came for, and an absent line would leave them
    counting rows for it.
    """
    blocked = sum(1 for row in rows if row.alerts_held_back)
    roots = sum(1 for row in rows if row.is_root_cause)
    parts = [f"{len(rows)} monitor(s) in the dependency graph", f"{blocked} held back"]
    if roots:
        parts.append(f"{roots} root cause(s)")
    return ", ".join(parts)
