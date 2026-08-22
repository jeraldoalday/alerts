"""Read-only database queries backing the dashboard routes."""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Sequence

from sqlalchemy import or_
from sqlalchemy.orm import Session

from alerts.models import CheckResult, Incident, IncidentState, Monitor
from alerts.timeutil import as_utc_naive


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
