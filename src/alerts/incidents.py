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
    state it held beforehand. Only the edges into and out of DOWN matter:
    entering DOWN opens a new incident, leaving DOWN resolves the open one.
    Repeated checks that don't cross a threshold, and the UP <-> DEGRADED
    slide, are no-ops here.

    Leaving DOWN counts as recovery whether the monitor lands on UP or on
    DEGRADED. A monitor whose recovery check is slow enough to be degraded is
    serving again, and treating that as "still down" would strand the
    incident open: the later DEGRADED -> UP transition doesn't come from
    DOWN, so nothing would ever resolve it.
    """
    if previous_state == monitor.state:
        return

    if monitor.state == MonitorState.DOWN:
        session.add(Incident(monitor_id=monitor.id, cause=cause))
    elif previous_state == MonitorState.DOWN and monitor.state in (
        MonitorState.UP,
        MonitorState.DEGRADED,
    ):
        open_incident = (
            session.query(Incident)
            .filter_by(monitor_id=monitor.id, state=IncidentState.OPEN)
            .order_by(Incident.started_at.desc())
            .first()
        )
        if open_incident is not None:
            open_incident.state = IncidentState.RESOLVED
            open_incident.resolved_at = _utcnow()
