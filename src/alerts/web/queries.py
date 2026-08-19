"""Read-only database queries backing the dashboard routes."""
from __future__ import annotations

from typing import List, Optional, Sequence

from sqlalchemy.orm import Session

from alerts.models import CheckResult, Incident, IncidentState, Monitor


def list_monitors(session: Session) -> Sequence[Monitor]:
    return session.query(Monitor).order_by(Monitor.name).all()


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


def count_open_incidents(session: Session) -> int:
    """How many incidents are currently open, across every monitor."""
    return session.query(Incident).filter_by(state=IncidentState.OPEN).count()
