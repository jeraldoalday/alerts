"""Retention pruning: age cutoffs, the per-monitor floor, and cascade behaviour."""
from datetime import datetime, timedelta, timezone

from alerts.config import RetentionConfig
from alerts.models import (
    CheckResult,
    Incident,
    IncidentState,
    Monitor,
    MonitorType,
    NotificationKind,
    NotificationLog,
)
from alerts.retention import (
    PruneSummary,
    prune,
    prune_check_results,
    prune_resolved_incidents,
)

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)


def _monitor(session, name: str = "site") -> Monitor:
    monitor = Monitor(name=name, type=MonitorType.HTTP, target="https://example.com")
    session.add(monitor)
    session.flush()
    return monitor


def _check(session, monitor_id: int, *, days_ago: float, success: bool = True) -> CheckResult:
    result = CheckResult(
        monitor_id=monitor_id,
        success=success,
        latency_ms=10.0,
        checked_at=NOW.replace(tzinfo=None) - timedelta(days=days_ago),
    )
    session.add(result)
    return result


def _resolved_incident(session, monitor_id: int, *, days_ago: float) -> Incident:
    incident = Incident(
        monitor_id=monitor_id,
        state=IncidentState.RESOLVED,
        started_at=NOW.replace(tzinfo=None) - timedelta(days=days_ago + 1),
        resolved_at=NOW.replace(tzinfo=None) - timedelta(days=days_ago),
    )
    session.add(incident)
    session.flush()
    return incident


def test_prunes_check_results_older_than_the_cutoff(db):
    with db.session() as session:
        monitor = _monitor(session)
        _check(session, monitor.id, days_ago=40)
        _check(session, monitor.id, days_ago=31)
        _check(session, monitor.id, days_ago=5)

    with db.session() as session:
        deleted = prune_check_results(session, older_than_days=30, now=NOW)

    assert deleted == 2
    with db.session() as session:
        assert session.query(CheckResult).count() == 1


def test_aware_now_matches_naive_stored_timestamps(db):
    """Regression guard: an aware cutoff must not compare as an offset-suffixed string."""
    with db.session() as session:
        monitor = _monitor(session)
        _check(session, monitor.id, days_ago=40)

    with db.session() as session:
        assert prune_check_results(session, older_than_days=30, now=NOW) == 1


def test_keeps_the_configured_floor_of_recent_checks(db):
    with db.session() as session:
        monitor = _monitor(session)
        for days_ago in range(40, 30, -1):
            _check(session, monitor.id, days_ago=days_ago)

    with db.session() as session:
        deleted = prune_check_results(
            session, older_than_days=30, min_per_monitor=3, now=NOW
        )

    assert deleted == 7
    with db.session() as session:
        remaining = session.query(CheckResult).order_by(CheckResult.checked_at).all()
        assert [r.checked_at for r in remaining] == [
            NOW.replace(tzinfo=None) - timedelta(days=days_ago) for days_ago in (33, 32, 31)
        ]


def test_floor_is_counted_per_monitor(db):
    with db.session() as session:
        first = _monitor(session, "first")
        second = _monitor(session, "second")
        for days_ago in (40, 39, 38):
            _check(session, first.id, days_ago=days_ago)
            _check(session, second.id, days_ago=days_ago)

    with db.session() as session:
        deleted = prune_check_results(
            session, older_than_days=30, min_per_monitor=2, now=NOW
        )

    assert deleted == 2
    with db.session() as session:
        assert session.query(CheckResult).count() == 4


def test_null_age_keeps_check_results_forever(db):
    with db.session() as session:
        monitor = _monitor(session)
        _check(session, monitor.id, days_ago=3650)

    with db.session() as session:
        assert prune_check_results(session, older_than_days=None, now=NOW) == 0
        assert session.query(CheckResult).count() == 1


def test_dry_run_counts_without_deleting(db):
    with db.session() as session:
        monitor = _monitor(session)
        _check(session, monitor.id, days_ago=40)
        _check(session, monitor.id, days_ago=39)

    with db.session() as session:
        counted = prune_check_results(session, older_than_days=30, now=NOW, dry_run=True)

    assert counted == 2
    with db.session() as session:
        assert session.query(CheckResult).count() == 2


def test_pruning_an_incident_takes_its_notification_logs_with_it(db):
    with db.session() as session:
        monitor = _monitor(session)
        incident = _resolved_incident(session, monitor.id, days_ago=100)
        session.add(
            NotificationLog(
                incident_id=incident.id,
                channel_name="ops-slack",
                kind=NotificationKind.OPENED,
                success=True,
            )
        )

    with db.session() as session:
        summary = prune_resolved_incidents(session, older_than_days=90, now=NOW)

    assert summary.incidents_deleted == 1
    assert summary.notification_logs_deleted == 1
    with db.session() as session:
        assert session.query(Incident).count() == 0
        assert session.query(NotificationLog).count() == 0


def test_open_incidents_are_never_pruned(db):
    with db.session() as session:
        monitor = _monitor(session)
        session.add(
            Incident(
                monitor_id=monitor.id,
                state=IncidentState.OPEN,
                started_at=NOW.replace(tzinfo=None) - timedelta(days=365),
            )
        )

    with db.session() as session:
        summary = prune_resolved_incidents(session, older_than_days=90, now=NOW)

    assert summary.incidents_deleted == 0
    with db.session() as session:
        assert session.query(Incident).count() == 1


def test_recent_resolved_incidents_are_kept(db):
    with db.session() as session:
        monitor = _monitor(session)
        _resolved_incident(session, monitor.id, days_ago=10)

    with db.session() as session:
        assert prune_resolved_incidents(session, older_than_days=90, now=NOW).incidents_deleted == 0


def test_null_age_keeps_resolved_incidents_forever(db):
    with db.session() as session:
        monitor = _monitor(session)
        _resolved_incident(session, monitor.id, days_ago=3650)

    with db.session() as session:
        summary = prune_resolved_incidents(session, older_than_days=None, now=NOW)

    assert summary == PruneSummary()
    with db.session() as session:
        assert session.query(Incident).count() == 1


def test_prune_applies_the_whole_policy(db):
    with db.session() as session:
        monitor = _monitor(session)
        _check(session, monitor.id, days_ago=40)
        _check(session, monitor.id, days_ago=1)
        incident = _resolved_incident(session, monitor.id, days_ago=100)
        session.add(
            NotificationLog(incident_id=incident.id, channel_name="ops-slack", success=True)
        )

    retention = RetentionConfig(
        check_results_days=30, resolved_incidents_days=90, min_check_results_per_monitor=0
    )
    with db.session() as session:
        summary = prune(session, retention, now=NOW)

    assert summary == PruneSummary(
        check_results_deleted=1, incidents_deleted=1, notification_logs_deleted=1
    )
    assert summary.total_deleted == 3
    assert "1 check result(s)" in summary.describe()


def test_prune_of_an_empty_database_is_a_no_op(db):
    with db.session() as session:
        summary = prune(session, RetentionConfig(), now=NOW)

    assert summary.total_deleted == 0


def test_check_results_orphaned_by_a_missing_monitor_are_still_pruned(db):
    """Rows are found via check_results, not via the monitors table."""
    with db.session() as session:
        monitor = _monitor(session)
        monitor_id = monitor.id
        _check(session, monitor_id, days_ago=40)

    with db.session() as session:
        session.query(Monitor).filter_by(id=monitor_id).delete(synchronize_session=False)

    with db.session() as session:
        assert prune_check_results(session, older_than_days=30, now=NOW) == 1
