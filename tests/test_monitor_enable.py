"""Enabling/disabling monitors as they come and go from the config.

`sync_monitors` keeps a monitor's row (and its history) when the config
stops declaring it, so the row has to be marked disabled instead -- otherwise
the scheduler, which reads its work from the table rather than the config,
carries on checking it forever.
"""
import asyncio

from alerts.checkers.base import CheckOutcome, CheckRequest
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


class _FailingChecker:
    """Always reports the target as broken, so a monitor stays DOWN."""

    def __init__(self):
        self.calls = 0

    async def check(self, request: CheckRequest) -> CheckOutcome:
        self.calls += 1
        return CheckOutcome(success=False, error_message="connection refused")


class _StubChecker:
    def __init__(self):
        self.calls = 0

    async def check(self, request: CheckRequest) -> CheckOutcome:
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


def test_re_enabling_resets_the_state_frozen_at_the_pause(db):
    with db.session() as session:
        session.add(
            _monitor(
                "site",
                enabled=False,
                state=MonitorState.DOWN,
                consecutive_failures=7,
                consecutive_successes=0,
                consecutive_fast_checks=0,
            )
        )

    with db.session() as session:
        sync_monitors(session, [_config("site")])

    with db.session() as session:
        monitor = session.query(Monitor).one()
        assert monitor.enabled is True
        assert monitor.state == MonitorState.UNKNOWN
        assert monitor.consecutive_failures == 0


def test_re_enabling_clears_a_banked_success_streak(db):
    """A streak earned before the pause says nothing about the target now."""
    with db.session() as session:
        session.add(
            _monitor(
                "site",
                enabled=False,
                state=MonitorState.UP,
                consecutive_successes=42,
                consecutive_fast_checks=42,
            )
        )

    with db.session() as session:
        sync_monitors(session, [_config("site")])

    with db.session() as session:
        monitor = session.query(Monitor).one()
        assert monitor.consecutive_successes == 0
        assert monitor.consecutive_fast_checks == 0


def test_an_active_monitor_keeps_its_state_across_a_sync(db):
    """Only the pause -> active edge resets; a plain config edit must not.

    `sync_monitors` runs on every `alerts run`, so resetting an enabled
    monitor here would throw away the streak of any monitor whose config was
    touched -- and restart the failure threshold of one that is mid-outage.
    """
    with db.session() as session:
        session.add(
            _monitor(
                "site",
                enabled=True,
                state=MonitorState.DOWN,
                consecutive_failures=5,
            )
        )

    with db.session() as session:
        sync_monitors(session, [_config("site", target="https://example.org")])

    with db.session() as session:
        monitor = session.query(Monitor).one()
        assert monitor.state == MonitorState.DOWN
        assert monitor.consecutive_failures == 5
        assert monitor.target == "https://example.org"


async def test_a_re_enabled_monitor_still_broken_opens_a_fresh_incident(db):
    """The bug this reset exists for, end to end.

    Pausing a DOWN monitor resolves its incident. Without the reset the row
    came back still marked DOWN, and since an incident is only opened on the
    transition *into* DOWN, a target that never recovered failed check after
    check with nothing opened and nobody paged.
    """
    with db.session() as session:
        session.add(
            _monitor(
                "site",
                enabled=True,
                state=MonitorState.DOWN,
                failure_threshold=1,
                interval_seconds=3600,
            )
        )
    with db.session() as session:
        sync_monitors(session, [])  # pause: resolves the open incident, if any
        sync_monitors(session, [_config("site", failure_threshold=1)])

    checker = _FailingChecker()
    scheduler = Scheduler(db, checkers={MonitorType.HTTP: checker})

    run_task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0.05)
    await scheduler.stop()
    await run_task

    with db.session() as session:
        incidents = session.query(Incident).all()
        assert len(incidents) == 1
        assert incidents[0].state == IncidentState.OPEN
