"""Enabling/disabling monitors as they come and go from the config.

`sync_monitors` keeps a monitor's row (and its history) when the config
stops declaring it, so the row has to be marked disabled instead -- otherwise
the scheduler, which reads its work from the table rather than the config,
carries on checking it forever.
"""
import asyncio

from alerts.checkers.base import CheckOutcome
from alerts.config import MonitorConfig
from alerts.models import (
    CheckResult,
    Incident,
    IncidentState,
    Monitor,
    MonitorState,
    MonitorType,
)
from alerts.scheduler import Scheduler, sync_monitors


def _config(name: str = "site", **overrides) -> MonitorConfig:
    defaults = dict(name=name, type="http", target="https://example.com")
    defaults.update(overrides)
    return MonitorConfig(**defaults)


def _monitor(name: str = "site", **overrides) -> Monitor:
    defaults = dict(
        name=name,
        type=MonitorType.HTTP,
        target="https://example.com",
        state=MonitorState.UP,
        consecutive_failures=0,
        consecutive_successes=0,
        consecutive_fast_checks=0,
        enabled=True,
    )
    defaults.update(overrides)
    return Monitor(**defaults)


class _StubChecker:
    def __init__(self):
        self.calls = 0

    async def check(self, *, target, timeout_seconds, expected_status):
        self.calls += 1
        return CheckOutcome(success=True, latency_ms=1.0, status_code=200)


def test_a_new_monitor_is_created_enabled(db):
    with db.session() as session:
        sync_monitors(session, [_config()])

    with db.session() as session:
        assert session.query(Monitor).one().enabled is True


def test_a_monitor_missing_from_the_config_is_disabled(db):
    with db.session() as session:
        sync_monitors(session, [_config("kept"), _config("dropped")])

    with db.session() as session:
        sync_monitors(session, [_config("kept")])

    with db.session() as session:
        by_name = {m.name: m for m in session.query(Monitor).all()}
        assert by_name["kept"].enabled is True
        assert by_name["dropped"].enabled is False


def test_disabling_keeps_the_row_and_its_history(db):
    with db.session() as session:
        session.add(_monitor("dropped"))
    with db.session() as session:
        monitor = session.query(Monitor).one()
        session.add(CheckResult(monitor_id=monitor.id, success=True, latency_ms=5.0))

    with db.session() as session:
        sync_monitors(session, [])

    with db.session() as session:
        assert session.query(Monitor).count() == 1
        assert session.query(CheckResult).count() == 1


def test_disabling_resolves_an_incident_left_open(db):
    """Nothing will check the monitor again, so nothing would ever resolve it."""
    with db.session() as session:
        session.add(_monitor("dropped", state=MonitorState.DOWN))
    with db.session() as session:
        monitor = session.query(Monitor).one()
        session.add(Incident(monitor_id=monitor.id, cause="boom"))

    with db.session() as session:
        sync_monitors(session, [])

    with db.session() as session:
        incident = session.query(Incident).one()
        assert incident.state == IncidentState.RESOLVED
        assert incident.resolved_at is not None


def test_a_monitor_returning_to_the_config_is_re_enabled(db):
    with db.session() as session:
        session.add(_monitor("site", enabled=False))
    with db.session() as session:
        monitor = session.query(Monitor).one()
        session.add(CheckResult(monitor_id=monitor.id, success=True, latency_ms=5.0))

    with db.session() as session:
        sync_monitors(session, [_config("site")])

    with db.session() as session:
        assert session.query(Monitor).one().enabled is True
        assert session.query(CheckResult).count() == 1, "history survived the round trip"


def test_already_resolved_incidents_are_not_touched_when_disabling(db):
    with db.session() as session:
        session.add(_monitor("dropped"))
    with db.session() as session:
        monitor = session.query(Monitor).one()
        session.add(
            Incident(monitor_id=monitor.id, state=IncidentState.RESOLVED, cause="old")
        )

    with db.session() as session:
        sync_monitors(session, [])

    with db.session() as session:
        assert session.query(Incident).one().resolved_at is None, "left exactly as found"


async def test_the_scheduler_skips_disabled_monitors(db):
    with db.session() as session:
        session.add(_monitor("enabled-one", enabled=True, interval_seconds=3600))
        session.add(_monitor("disabled-one", enabled=False, interval_seconds=3600))

    checker = _StubChecker()
    scheduler = Scheduler(db, checkers={MonitorType.HTTP: checker})

    run_task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0.05)
    await scheduler.stop()
    await run_task

    assert checker.calls == 1, "only the enabled monitor was checked"
    with db.session() as session:
        results = session.query(CheckResult).all()
        monitors = {m.id: m.name for m in session.query(Monitor).all()}
        assert [monitors[r.monitor_id] for r in results] == ["enabled-one"]


async def test_a_monitor_disabled_mid_run_stops_being_checked(db):
    """The loop re-reads the flag each interval, so no restart is needed."""
    with db.session() as session:
        session.add(_monitor("site", interval_seconds=5))

    checker = _StubChecker()
    scheduler = Scheduler(db, checkers={MonitorType.HTTP: checker})

    run_task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0.05)
    with db.session() as session:
        sync_monitors(session, [])
    await scheduler.stop()
    await run_task

    assert checker.calls == 1
