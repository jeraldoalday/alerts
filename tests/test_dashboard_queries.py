from datetime import datetime, timedelta, timezone

from alerts.models import CheckResult, Incident, Monitor, MonitorState, MonitorType
from alerts.web import queries


def _monitor(session, **overrides) -> Monitor:
    defaults = dict(name="site", type=MonitorType.HTTP, target="https://example.com")
    defaults.update(overrides)
    monitor = Monitor(**defaults)
    session.add(monitor)
    session.flush()
    return monitor


def test_list_monitors_orders_by_name(db):
    with db.session() as session:
        _monitor(session, name="zeta")
        _monitor(session, name="alpha")
        _monitor(session, name="mid")

    with db.session() as session:
        names = [m.name for m in queries.list_monitors(session)]

    assert names == ["alpha", "mid", "zeta"]


def test_get_monitor_by_name_returns_match(db):
    with db.session() as session:
        _monitor(session, name="site")

    with db.session() as session:
        monitor = queries.get_monitor_by_name(session, "site")
        assert monitor is not None
        assert monitor.name == "site"


def test_get_monitor_by_name_returns_none_when_missing(db):
    with db.session() as session:
        monitor = queries.get_monitor_by_name(session, "does-not-exist")

    assert monitor is None


def test_recent_check_results_orders_newest_first_and_respects_limit(db):
    with db.session() as session:
        monitor = _monitor(session)
        base = datetime.now(timezone.utc)
        for i in range(5):
            session.add(
                CheckResult(
                    monitor_id=monitor.id,
                    success=True,
                    checked_at=base + timedelta(minutes=i),
                )
            )
        monitor_id = monitor.id

    with db.session() as session:
        results = queries.recent_check_results(session, monitor_id, limit=3)

    assert len(results) == 3
    assert [r.checked_at for r in results] == sorted(
        (r.checked_at for r in results), reverse=True
    )


def test_recent_check_results_only_returns_matching_monitor(db):
    with db.session() as session:
        monitor_a = _monitor(session, name="a")
        monitor_b = _monitor(session, name="b")
        session.add(CheckResult(monitor_id=monitor_a.id, success=True))
        session.add(CheckResult(monitor_id=monitor_b.id, success=False))
        monitor_a_id = monitor_a.id

    with db.session() as session:
        results = queries.recent_check_results(session, monitor_a_id)

    assert len(results) == 1
    assert results[0].monitor_id == monitor_a_id


def test_recent_incidents_orders_newest_first_and_respects_limit(db):
    with db.session() as session:
        monitor = _monitor(session, state=MonitorState.DOWN)
        base = datetime.now(timezone.utc)
        for i in range(4):
            session.add(
                Incident(monitor_id=monitor.id, cause="down", started_at=base + timedelta(hours=i))
            )
        monitor_id = monitor.id

    with db.session() as session:
        incidents = queries.recent_incidents(session, monitor_id, limit=2)

    assert len(incidents) == 2
    assert incidents[0].started_at > incidents[1].started_at
