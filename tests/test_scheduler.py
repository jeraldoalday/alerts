import asyncio

from alerts.checkers.base import CheckOutcome
from alerts.config import MonitorConfig
from alerts.models import CheckResult, Monitor, MonitorState, MonitorType
from alerts.scheduler import Scheduler, apply_check_outcome, sync_monitors


def _monitor(**overrides) -> Monitor:
    defaults = dict(
        name="site",
        type=MonitorType.HTTP,
        target="https://example.com",
        failure_threshold=2,
        recovery_threshold=2,
        consecutive_failures=0,
        consecutive_successes=0,
    )
    defaults.update(overrides)
    return Monitor(**defaults)


def test_apply_check_outcome_marks_down_after_failure_threshold():
    monitor = _monitor(state=MonitorState.UP, consecutive_successes=5)

    apply_check_outcome(monitor, CheckOutcome(success=False, error_message="boom"))
    assert monitor.state == MonitorState.UP
    assert monitor.consecutive_failures == 1
    assert monitor.consecutive_successes == 0

    apply_check_outcome(monitor, CheckOutcome(success=False, error_message="boom"))
    assert monitor.state == MonitorState.DOWN
    assert monitor.consecutive_failures == 2


def test_apply_check_outcome_marks_up_after_recovery_threshold():
    monitor = _monitor(state=MonitorState.DOWN, consecutive_failures=5)

    apply_check_outcome(monitor, CheckOutcome(success=True, latency_ms=12.0, status_code=200))
    assert monitor.state == MonitorState.DOWN
    assert monitor.consecutive_successes == 1

    apply_check_outcome(monitor, CheckOutcome(success=True, latency_ms=12.0, status_code=200))
    assert monitor.state == MonitorState.UP


def test_sync_monitors_creates_and_updates_by_name(db):
    configs = [
        MonitorConfig(name="site", type="http", target="https://a.example.com", interval_seconds=30)
    ]
    with db.session() as session:
        sync_monitors(session, configs)

    with db.session() as session:
        monitor = session.query(Monitor).filter_by(name="site").one()
        assert monitor.target == "https://a.example.com"
        assert monitor.interval_seconds == 30

    configs = [
        MonitorConfig(name="site", type="http", target="https://b.example.com", interval_seconds=15)
    ]
    with db.session() as session:
        sync_monitors(session, configs)

    with db.session() as session:
        monitors = session.query(Monitor).all()
        assert len(monitors) == 1
        assert monitors[0].target == "https://b.example.com"
        assert monitors[0].interval_seconds == 15


class _FakeChecker:
    def __init__(self, outcome: CheckOutcome):
        self._outcome = outcome
        self.calls = 0

    async def check(self, **kwargs) -> CheckOutcome:
        self.calls += 1
        return self._outcome


async def test_scheduler_runs_check_and_persists_result(db):
    with db.session() as session:
        session.add(_monitor(interval_seconds=60))

    fake = _FakeChecker(CheckOutcome(success=True, latency_ms=5.0, status_code=200))
    scheduler = Scheduler(db, checkers={MonitorType.HTTP: fake})

    run_task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0.05)
    await scheduler.stop()
    await run_task

    assert fake.calls == 1
    with db.session() as session:
        results = session.query(CheckResult).all()
        assert len(results) == 1
        assert results[0].success is True

        monitor = session.query(Monitor).filter_by(name="site").one()
        assert monitor.consecutive_successes == 1


async def test_scheduler_uses_the_checker_matching_monitor_type(db):
    with db.session() as session:
        session.add(
            _monitor(name="tcp-thing", type=MonitorType.TCP, target="127.0.0.1:1", interval_seconds=60)
        )

    scheduler = Scheduler(db)  # default checkers include a real TcpChecker

    run_task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0.05)
    await scheduler.stop()
    await run_task

    with db.session() as session:
        result = session.query(CheckResult).one()
        # Port 1 on localhost is not listening, so the real TCP checker fails.
        assert result.success is False
        assert result.error_message
