"""Renders a human-readable message for one notification decision."""
from __future__ import annotations

from alerts.models import Incident, Monitor, NotificationKind
from alerts.timeutil import as_utc_naive, format_duration


def format_message(monitor: Monitor, incident: Incident, kind: NotificationKind) -> str:
    if kind == NotificationKind.OPENED:
        cause = incident.cause or "no details"
        return f"[DOWN] {monitor.name} ({monitor.target}) is down: {cause}"
    if kind == NotificationKind.ESCALATION:
        cause = incident.cause or "no details"
        return (
            f"[STILL DOWN] {monitor.name} ({monitor.target}) has been down since "
            f"{incident.started_at.isoformat()}: {cause}"
        )
    # RESOLVED. `resolved_at` is set by `sync_incident` before this is ever
    # called for a real incident, but an incomplete row (built directly in a
    # test, or recovered from some future bug in that ordering) still gets a
    # message rather than raising -- just one with no duration to report.
    if incident.resolved_at is None:
        return f"[RECOVERED] {monitor.name} ({monitor.target}) is back up"
    down_for = format_duration(
        (as_utc_naive(incident.resolved_at) - as_utc_naive(incident.started_at)).total_seconds()
    )
    return f"[RECOVERED] {monitor.name} ({monitor.target}) is back up after {down_for} down"
