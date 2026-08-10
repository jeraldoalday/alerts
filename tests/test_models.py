import pytest
from sqlalchemy.exc import IntegrityError

from alerts.models import (
    CheckResult,
    Incident,
    IncidentState,
    Monitor,
    MonitorState,
    MonitorType,
    NotificationLog,
)


def test_create_monitor(db):
    with db.session() as session:
        monitor = Monitor(
            name="example-website",
            type=MonitorType.HTTP,
            target="https://example.com",
        )
        session.add(monitor)

    with db.session() as session:
        stored = session.query(Monitor).filter_by(name="example-website").one()
        assert stored.type == MonitorType.HTTP
        assert stored.state == MonitorState.UNKNOWN
        assert stored.consecutive_failures == 0


def test_check_results_relate_to_monitor(db):
    with db.session() as session:
        monitor = Monitor(name="site", type=MonitorType.HTTP, target="https://x.example.com")
        session.add(monitor)
        session.flush()
        session.add(CheckResult(monitor_id=monitor.id, success=True, latency_ms=42.5, status_code=200))
        session.add(CheckResult(monitor_id=monitor.id, success=False, error_message="timeout"))

    with db.session() as session:
        monitor = session.query(Monitor).filter_by(name="site").one()
        assert len(monitor.check_results) == 2
        outcomes = {r.success for r in monitor.check_results}
        assert outcomes == {True, False}


def test_incident_lifecycle_and_notification_log(db):
    with db.session() as session:
        monitor = Monitor(name="site", type=MonitorType.HTTP, target="https://x.example.com")
        session.add(monitor)
        session.flush()

        incident = Incident(monitor_id=monitor.id, cause="3 consecutive failures")
        session.add(incident)
        session.flush()

        session.add(
            NotificationLog(incident_id=incident.id, channel_name="ops-slack", success=True)
        )

    with db.session() as session:
        incident = session.query(Incident).one()
        assert incident.state == IncidentState.OPEN
        assert incident.resolved_at is None
        assert len(incident.notifications) == 1
        assert incident.notifications[0].channel_name == "ops-slack"

        incident.state = IncidentState.RESOLVED

    with db.session() as session:
        incident = session.query(Incident).one()
        assert incident.state == IncidentState.RESOLVED


def test_monitor_name_must_be_unique(db):
    with db.session() as session:
        session.add(Monitor(name="dup", type=MonitorType.HTTP, target="https://a.example.com"))

    with pytest.raises(IntegrityError):
        with db.session() as session:
            session.add(Monitor(name="dup", type=MonitorType.HTTP, target="https://b.example.com"))
