"""Opens and resolves Incident records to track monitor downtime spans.

Incident lifecycle sits on top of `Monitor.state`: it doesn't decide when a
monitor is up or down (the scheduler's `apply_check_outcome` does that), it
just mirrors DOWN/UP transitions into a durable incident record so history,
duration, and notification dedup have something to key off.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from alerts.models import Incident, IncidentState, Monitor, MonitorState


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def sync_incident(
    session: Session,
    monitor: Monitor,
    previous_state: MonitorState,
    cause: Optional[str] = None,
) -> None:
    """Open or resolve an incident for a monitor's state transition.

    Call after `apply_check_outcome` has updated `monitor.state`, passing the
    state it held beforehand. Only the DOWN/UP edges matter: entering DOWN
    opens a new incident, returning to UP from DOWN resolves the open one.
    Transitions through/into DEGRADED, and repeated checks that don't cross a
    threshold, are no-ops here.
    """
    if previous_state == monitor.state:
        return

    if monitor.state == MonitorState.DOWN:
        session.add(Incident(monitor_id=monitor.id, cause=cause))
    elif monitor.state == MonitorState.UP and previous_state == MonitorState.DOWN:
        open_incident = (
            session.query(Incident)
            .filter_by(monitor_id=monitor.id, state=IncidentState.OPEN)
            .order_by(Incident.started_at.desc())
            .first()
        )
        if open_incident is not None:
            open_incident.state = IncidentState.RESOLVED
            open_incident.resolved_at = _utcnow()
