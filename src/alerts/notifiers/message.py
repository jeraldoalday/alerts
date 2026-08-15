"""Renders a human-readable message for one notification decision."""
from __future__ import annotations

from alerts.models import Incident, Monitor, NotificationKind


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
    return f"[RECOVERED] {monitor.name} ({monitor.target}) is back up"
