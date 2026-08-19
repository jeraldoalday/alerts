from datetime import datetime, timedelta, timezone

from alerts.models import (
    Incident,
    IncidentState,
    Monitor,
    MonitorType,
    NotificationKind,
    NotificationLog,
)
from alerts.notifications import NotificationDecision, decide_notifications

NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)


def _monitor(session, **overrides) -> Monitor:
    defaults = dict(name="site", type=MonitorType.HTTP, target="https://example.com")
    defaults.update(overrides)
    monitor = Monitor(**defaults)
    session.add(monitor)
    session.flush()
    return monitor


def _open_incident(session, **overrides) -> Incident:
    monitor = _monitor(session)
    defaults = dict(monitor_id=monitor.id, state=IncidentState.OPEN)
    defaults.update(overrides)
    incident = Incident(**defaults)
    session.add(incident)
    session.flush()
    return incident


def _log(session, incident, *, channel_name, kind, sent_at, success=True):
    session.add(
        NotificationLog(
            incident_id=incident.id,
            channel_name=channel_name,
            kind=kind,
            sent_at=sent_at,
            success=success,
        )
    )


def test_no_channels_yields_no_decisions(db):
    with db.session() as session:
        incident = _open_incident(session)
        assert decide_notifications(incident, [], cooldown_seconds=300, now=NOW) == []


def test_fresh_open_incident_notifies_every_channel_once(db):
    with db.session() as session:
        incident = _open_incident(session)
        decisions = decide_notifications(
            incident, ["ops-slack", "on-call-email"], cooldown_seconds=300, now=NOW
        )

    assert set(decisions) == {
        NotificationDecision("ops-slack", NotificationKind.OPENED),
        NotificationDecision("on-call-email", NotificationKind.OPENED),
    }


def test_already_notified_channel_is_deduped_within_cooldown(db):
    with db.session() as session:
        incident = _open_incident(session)
        _log(session, incident, channel_name="ops-slack", kind=NotificationKind.OPENED, sent_at=NOW)
        session.flush()

        decisions = decide_notifications(incident, ["ops-slack"], cooldown_seconds=300, now=NOW)

    assert decisions == []


def test_escalation_fires_once_cooldown_elapses(db):
    with db.session() as session:
        incident = _open_incident(session)
        _log(
            session,
            incident,
            channel_name="ops-slack",
            kind=NotificationKind.OPENED,
            sent_at=NOW - timedelta(seconds=301),
        )
        session.flush()

        decisions = decide_notifications(incident, ["ops-slack"], cooldown_seconds=300, now=NOW)

    assert decisions == [NotificationDecision("ops-slack", NotificationKind.ESCALATION)]


def test_escalation_keeps_repeating_on_the_same_cooldown(db):
    with db.session() as session:
        incident = _open_incident(session)
        _log(
            session,
            incident,
            channel_name="ops-slack",
            kind=NotificationKind.OPENED,
            sent_at=NOW - timedelta(seconds=700),
        )
        _log(
            session,
            incident,
            channel_name="ops-slack",
            kind=NotificationKind.ESCALATION,
            sent_at=NOW - timedelta(seconds=301),
        )
        session.flush()

        decisions = decide_notifications(incident, ["ops-slack"], cooldown_seconds=300, now=NOW)

    assert decisions == [NotificationDecision("ops-slack", NotificationKind.ESCALATION)]


def test_failed_send_is_not_deduped_and_is_retried(db):
    with db.session() as session:
        incident = _open_incident(session)
        _log(
            session,
            incident,
            channel_name="ops-slack",
            kind=NotificationKind.OPENED,
            sent_at=NOW,
            success=False,
        )
        session.flush()

        decisions = decide_notifications(incident, ["ops-slack"], cooldown_seconds=300, now=NOW)

    assert decisions == [NotificationDecision("ops-slack", NotificationKind.OPENED)]


def test_mixed_channels_only_the_uncooled_one_escalates(db):
    with db.session() as session:
        incident = _open_incident(session)
        _log(
            session,
            incident,
            channel_name="ops-slack",
            kind=NotificationKind.OPENED,
            sent_at=NOW - timedelta(seconds=301),
        )
        session.flush()

        decisions = decide_notifications(
            incident, ["ops-slack", "on-call-email"], cooldown_seconds=300, now=NOW
        )

    assert set(decisions) == {
        NotificationDecision("ops-slack", NotificationKind.ESCALATION),
        NotificationDecision("on-call-email", NotificationKind.OPENED),
    }


def test_resolved_incident_notifies_once_regardless_of_cooldown(db):
    with db.session() as session:
        incident = _open_incident(session, state=IncidentState.RESOLVED)
        _log(
            session,
            incident,
            channel_name="ops-slack",
            kind=NotificationKind.OPENED,
            sent_at=NOW,
        )
        session.flush()

        decisions = decide_notifications(incident, ["ops-slack"], cooldown_seconds=300, now=NOW)

    assert decisions == [NotificationDecision("ops-slack", NotificationKind.RESOLVED)]


def test_resolved_incident_already_notified_is_deduped(db):
    with db.session() as session:
        incident = _open_incident(session, state=IncidentState.RESOLVED)
        _log(session, incident, channel_name="ops-slack", kind=NotificationKind.OPENED, sent_at=NOW)
        _log(
            session,
            incident,
            channel_name="ops-slack",
            kind=NotificationKind.RESOLVED,
            sent_at=NOW,
        )
        session.flush()

        decisions = decide_notifications(incident, ["ops-slack"], cooldown_seconds=300, now=NOW)

    assert decisions == []


def test_a_channel_never_told_it_opened_is_not_told_it_resolved(db):
    """Otherwise a channel added mid-incident hears only "[RECOVERED]"."""
    with db.session() as session:
        incident = _open_incident(session, state=IncidentState.RESOLVED)
        session.flush()

        decisions = decide_notifications(incident, ["ops-slack"], cooldown_seconds=300, now=NOW)

    assert decisions == []


def test_a_failed_open_send_does_not_earn_a_resolved_notice(db):
    with db.session() as session:
        incident = _open_incident(session, state=IncidentState.RESOLVED)
        _log(
            session,
            incident,
            channel_name="ops-slack",
            kind=NotificationKind.OPENED,
            sent_at=NOW,
            success=False,
        )
        session.flush()

        decisions = decide_notifications(incident, ["ops-slack"], cooldown_seconds=300, now=NOW)

    assert decisions == []


def test_an_escalated_channel_is_told_it_resolved(db):
    """The escalation is proof the channel knows about the incident."""
    with db.session() as session:
        incident = _open_incident(session, state=IncidentState.RESOLVED)
        _log(
            session,
            incident,
            channel_name="ops-slack",
            kind=NotificationKind.ESCALATION,
            sent_at=NOW,
        )
        session.flush()

        decisions = decide_notifications(incident, ["ops-slack"], cooldown_seconds=300, now=NOW)

    assert decisions == [NotificationDecision("ops-slack", NotificationKind.RESOLVED)]


def test_only_the_channel_that_was_paged_is_told_it_resolved(db):
    with db.session() as session:
        incident = _open_incident(session, state=IncidentState.RESOLVED)
        _log(
            session,
            incident,
            channel_name="ops-slack",
            kind=NotificationKind.OPENED,
            sent_at=NOW,
        )
        session.flush()

        decisions = decide_notifications(
            incident, ["ops-slack", "ops-email"], cooldown_seconds=300, now=NOW
        )

    assert decisions == [NotificationDecision("ops-slack", NotificationKind.RESOLVED)]
