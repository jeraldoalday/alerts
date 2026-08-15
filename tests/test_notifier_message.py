from datetime import datetime, timezone

from alerts.models import Incident, Monitor, MonitorType, NotificationKind
from alerts.notifiers.message import format_message


def _monitor(**overrides) -> Monitor:
    defaults = dict(name="main-site", type=MonitorType.HTTP, target="https://example.com")
    defaults.update(overrides)
    return Monitor(**defaults)


def _incident(**overrides) -> Incident:
    defaults = dict(
        cause="connection refused",
        started_at=datetime(2026, 8, 15, 10, 0, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return Incident(**defaults)


def test_opened_message_includes_monitor_and_cause():
    message = format_message(_monitor(), _incident(), NotificationKind.OPENED)

    assert "main-site" in message
    assert "https://example.com" in message
    assert "connection refused" in message
    assert "DOWN" in message


def test_opened_message_falls_back_when_cause_missing():
    message = format_message(_monitor(), _incident(cause=None), NotificationKind.OPENED)

    assert "no details" in message


def test_escalation_message_includes_started_at():
    message = format_message(_monitor(), _incident(), NotificationKind.ESCALATION)

    assert "STILL DOWN" in message
    assert "2026-08-15T10:00:00+00:00" in message


def test_resolved_message_has_no_cause():
    message = format_message(_monitor(), _incident(), NotificationKind.RESOLVED)

    assert "RECOVERED" in message
    assert "connection refused" not in message
