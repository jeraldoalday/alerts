"""Read-only database queries backing the dashboard routes."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Sequence

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from alerts.models import CheckResult, Incident, IncidentState, Monitor
from alerts.slo import ErrorBudget, compute_error_budget
from alerts.timeutil import as_utc_naive, utcnow


def list_monitors(session: Session, *, tag: Optional[str] = None) -> Sequence[Monitor]:
    """Every monitor by name, optionally narrowed to one tag.

    The tag filter is applied in Python rather than in SQL. Tags live in a
    JSON column, and the containment operators for those differ by backend
    (and don't exist at all on older SQLite builds), so pushing the filter
    down would trade a portable query for one that fails on some installs.
    The cost is reading rows that are then discarded, over a table that holds
    one row per monitored service -- tens of them, not millions.
    """
    monitors = session.query(Monitor).order_by(Monitor.name).all()
    if tag is None:
        return monitors
    return [monitor for monitor in monitors if monitor.has_tag(tag)]


def count_monitors(session: Session) -> int:
    """How many monitors the database holds, paused ones included.

    Distinguishes an install whose config has never been synced from one
    whose monitors simply have nothing to report: the two produce the same
    empty report, and only one of them is a problem the operator can fix.
    """
    return session.query(Monitor).count()


def known_tags(session: Session) -> List[str]:
    """Every tag any monitor carries, sorted, without repeats.

    Read from the monitors table rather than from the config so it covers
    paused monitors too: a tag that only survives on a monitor the config no
    longer declares still appears on the dashboard, and offering a filter
    that silently matches nothing would be worse than listing it.
    """
    found = {tag for monitor in session.query(Monitor).all() for tag in monitor.tags or []}
    return sorted(found)


def get_monitor_by_name(session: Session, name: str) -> Optional[Monitor]:
    return session.query(Monitor).filter_by(name=name).first()


def recent_check_results(session: Session, monitor_id: int, limit: int = 50) -> List[CheckResult]:
    return (
        session.query(CheckResult)
        .filter_by(monitor_id=monitor_id)
        .order_by(CheckResult.checked_at.desc())
        .limit(limit)
        .all()
    )


def recent_incidents(session: Session, monitor_id: int, limit: int = 20) -> List[Incident]:
    return (
        session.query(Incident)
        .filter_by(monitor_id=monitor_id)
        .order_by(Incident.started_at.desc())
        .limit(limit)
        .all()
    )


def list_incidents(
    session: Session,
    *,
    monitor_id: Optional[int] = None,
    since: Optional[datetime] = None,
    open_only: bool = False,
    limit: int = 50,
) -> List[Incident]:
    """Incidents across every monitor, newest first.

    Unlike `recent_incidents`, which answers "what has happened to this one
    service", this is the estate-wide view an incident report is built from,
    so the monitor filter is optional and a time window is available.

    `since` is normalised to naive UTC before it is bound: the stored
    timestamps come back from SQLite without tzinfo, and comparing them
    against an aware value compares ISO strings with a "+00:00" suffix
    against ones without it, which silently puts rows on the wrong side of
    the cutoff.

    An incident is included by when it *started*, not by when it resolved, so
    a window covers the outages that began in it rather than the ones that
    happened to end in it.

    Incidents that are still open ignore the window entirely. An outage that
    began three weeks ago and has not recovered is the single most important
    row in any report, and dropping it out of a seven-day view because of its
    start date would answer "nothing has been broken lately" about a service
    that is broken right now.
    """
    query = session.query(Incident)
    if monitor_id is not None:
        query = query.filter(Incident.monitor_id == monitor_id)
    if since is not None:
        query = query.filter(
            or_(
                Incident.started_at >= as_utc_naive(since),
                Incident.state == IncidentState.OPEN,
            )
        )
    if open_only:
        query = query.filter(Incident.state == IncidentState.OPEN)
    return query.order_by(Incident.started_at.desc(), Incident.id.desc()).limit(limit).all()


def count_open_incidents(session: Session) -> int:
    """How many incidents are currently open, across every monitor."""
    return session.query(Incident).filter_by(state=IncidentState.OPEN).count()


@dataclass(frozen=True)
class CheckTally:
    """How many checks a window holds, and how many of them failed.

    Two counts rather than the rows themselves. An SLO window is measured in
    days, not in the fifty-row page the dashboard shows, so a 30-day window
    on a 15-second interval is 172,800 rows -- loading them to length-check
    two lists would be a very large read for two small numbers.
    """

    total: int
    failed: int

    @property
    def successful(self) -> int:
        return self.total - self.failed


def check_tally_since(
    session: Session, monitor_id: int, since: Optional[datetime] = None
) -> CheckTally:
    """Count one monitor's checks, and its failures, since a cutoff.

    `since` is normalised to naive UTC before it is bound, for the reason
    spelled out on `list_incidents`: SQLite stores datetimes as ISO strings,
    so an aware value compares a "+00:00" suffix against strings that don't
    carry one and silently puts rows on the wrong side of the cutoff.

    A `None` cutoff counts everything still retained. That is not what an
    SLO window wants -- a promise over "however much history happens to be
    kept" is not a promise -- but it is what a caller asking for a lifetime
    figure means, and refusing it here would push the special case outward.
    """
    counts = session.query(func.count(CheckResult.id)).filter(
        CheckResult.monitor_id == monitor_id
    )
    if since is not None:
        counts = counts.filter(CheckResult.checked_at >= as_utc_naive(since))
    total = counts.scalar() or 0
    failed = counts.filter(CheckResult.success.is_(False)).scalar() or 0
    return CheckTally(total=total, failed=failed)


def error_budget(
    session: Session, monitor: Monitor, *, now: Optional[datetime] = None
) -> Optional[ErrorBudget]:
    """The monitor's error budget over its own SLO window, or None without one.

    The single place a monitor row is turned into a budget, so the report,
    the dashboard, the JSON API, and the metrics exposition can't end up
    measuring the same promise over different windows.

    None rather than a zero-target budget for a monitor with no objective:
    there is nothing to report, and inventing a target to report it against
    would be the one way a service could appear to be meeting a promise
    nobody made.
    """
    if monitor.slo_target is None:
        return None
    moment = as_utc_naive(now or utcnow())
    tally = check_tally_since(
        session, monitor.id, moment - timedelta(days=monitor.slo_window_days)
    )
    return compute_error_budget(
        target_percentage=monitor.slo_target,
        total_checks=tally.total,
        failed_checks=tally.failed,
        window_days=monitor.slo_window_days,
    )


def error_budgets(
    session: Session, monitors: Iterable[Monitor], *, now: Optional[datetime] = None
) -> Dict[str, ErrorBudget]:
    """Budgets for a set of monitors, keyed by name, skipping those without one.

    A mapping rather than a list aligned with the monitors, because that is
    the shape both consumers want: the index table and the metrics
    exposition each walk their own rows and look a budget up per monitor,
    and a monitor with no objective is simply absent -- an em dash on the
    page, no `_slo_` series in the exposition.

    Names are the key because they are unique by database constraint and are
    what a rendering already has in hand; the id would mean every caller
    carrying a second lookup table to get back to it.
    """
    budgets: Dict[str, ErrorBudget] = {}
    for monitor in monitors:
        budget = error_budget(session, monitor, now=now)
        if budget is not None:
            budgets[monitor.name] = budget
    return budgets
