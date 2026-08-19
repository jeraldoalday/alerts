import asyncio

from alerts.checkers.base import CheckOutcome
from alerts.checkers.http import HttpChecker
from alerts.config import MonitorConfig
from alerts.models import (
    CheckResult,
    Incident,
    IncidentState,
    Monitor,
    MonitorState,
    MonitorType,
    NotificationKind,
    NotificationLog,
)
from alerts.notifiers.base import NotifyResult
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
        consecutive_fast_checks=0,
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


def test_sync_monitors_persists_channel_refs_on_create_and_update(db):
    configs = [
        MonitorConfig(
            name="site", type="http", target="https://a.example.com", channels=["ops-slack"]
        )
    ]
    with db.session() as session:
        sync_monitors(session, configs)

    with db.session() as session:
        monitor = session.query(Monitor).filter_by(name="site").one()
        assert monitor.channels == ["ops-slack"]

    configs = [
        MonitorConfig(
            name="site",
            type="http",
            target="https://a.example.com",
            channels=["ops-slack", "ops-email"],
        )
    ]
    with db.session() as session:
        sync_monitors(session, configs)

    with db.session() as session:
        monitor = session.query(Monitor).filter_by(name="site").one()
        assert monitor.channels == ["ops-slack", "ops-email"]


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


async def test_scheduler_opens_incident_when_monitor_crosses_failure_threshold(db):
    with db.session() as session:
        session.add(_monitor(interval_seconds=60, failure_threshold=1))

    fake = _FakeChecker(CheckOutcome(success=False, error_message="connection refused"))
    scheduler = Scheduler(db, checkers={MonitorType.HTTP: fake})

    run_task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0.05)
    await scheduler.stop()
    await run_task

    with db.session() as session:
        monitor = session.query(Monitor).filter_by(name="site").one()
        assert monitor.state == MonitorState.DOWN

        incident = session.query(Incident).one()
        assert incident.monitor_id == monitor.id
        assert incident.state == IncidentState.OPEN
        assert incident.cause == "connection refused"


class _DeletingChecker:
    """A checker whose `check()` deletes the monitor mid-flight.

    Exercises the scheduler's defensive `monitor is None` guard that fires
    when a monitor is removed between the check running and its result
    being persisted -- a race that's otherwise nearly impossible to hit
    deterministically in a test.
    """

    def __init__(self, database):
        self._database = database

    async def check(self, **kwargs) -> CheckOutcome:
        with self._database.session() as session:
            session.query(Monitor).delete()
        return CheckOutcome(success=True, latency_ms=1.0, status_code=200)


async def test_scheduler_skips_persisting_result_when_monitor_deleted_during_check(db):
    with db.session() as session:
        session.add(_monitor(interval_seconds=60))

    checker = _DeletingChecker(db)
    scheduler = Scheduler(db, checkers={MonitorType.HTTP: checker})

    run_task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0.05)
    await scheduler.stop()
    await run_task

    with db.session() as session:
        assert session.query(CheckResult).count() == 0


async def test_scheduler_runs_checks_again_after_interval_elapses(db):
    with db.session() as session:
        session.add(_monitor(interval_seconds=1))

    fake = _FakeChecker(CheckOutcome(success=True, latency_ms=1.0, status_code=200))
    scheduler = Scheduler(db, checkers={MonitorType.HTTP: fake})

    run_task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(1.2)
    await scheduler.stop()
    await run_task

    assert fake.calls >= 2


async def test_scheduler_loop_exits_cleanly_if_monitor_is_deleted(db):
    with db.session() as session:
        session.add(_monitor(interval_seconds=1))

    fake = _FakeChecker(CheckOutcome(success=True, latency_ms=1.0, status_code=200))
    scheduler = Scheduler(db, checkers={MonitorType.HTTP: fake})

    run_task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0.05)

    with db.session() as session:
        session.query(Monitor).delete()

    # After its 1s sleep, the loop notices the monitor is gone and returns on
    # its own; run() (an asyncio.gather of the per-monitor loops) completes
    # without stop() ever being called.
    await asyncio.wait_for(run_task, timeout=2)


async def test_scheduler_stop_closes_checkers_that_own_a_client(db, monkeypatch):
    with db.session() as session:
        session.add(_monitor(interval_seconds=60))

    checker = HttpChecker()

    async def fake_check(**kwargs) -> CheckOutcome:
        return CheckOutcome(success=True, latency_ms=1.0, status_code=200)

    monkeypatch.setattr(checker, "check", fake_check)
    scheduler = Scheduler(db, checkers={MonitorType.HTTP: checker})

    run_task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0.05)
    await scheduler.stop()
    await run_task

    assert checker._client.is_closed is True


class _FakeNotifier:
    def __init__(self, result: NotifyResult = NotifyResult(success=True)):
        self._result = result
        self.messages_sent = []
        self.closed = False

    async def send(self, message: str) -> NotifyResult:
        self.messages_sent.append(message)
        return self._result

    async def aclose(self) -> None:
        self.closed = True


async def test_scheduler_notifies_configured_channel_when_incident_opens(db):
    with db.session() as session:
        session.add(_monitor(interval_seconds=60, failure_threshold=1, channels=["ops-slack"]))

    fake_checker = _FakeChecker(CheckOutcome(success=False, error_message="connection refused"))
    fake_notifier = _FakeNotifier()
    scheduler = Scheduler(
        db, checkers={MonitorType.HTTP: fake_checker}, notifiers={"ops-slack": fake_notifier}
    )

    run_task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0.05)
    await scheduler.stop()
    await run_task

    assert len(fake_notifier.messages_sent) == 1
    assert "site" in fake_notifier.messages_sent[0]

    with db.session() as session:
        log = session.query(NotificationLog).one()
        assert log.channel_name == "ops-slack"
        assert log.kind == NotificationKind.OPENED
        assert log.success is True


async def test_scheduler_logs_failed_notification_send(db):
    with db.session() as session:
        session.add(_monitor(interval_seconds=60, failure_threshold=1, channels=["ops-slack"]))

    fake_checker = _FakeChecker(CheckOutcome(success=False, error_message="connection refused"))
    fake_notifier = _FakeNotifier(NotifyResult(success=False, error_message="webhook unreachable"))
    scheduler = Scheduler(
        db, checkers={MonitorType.HTTP: fake_checker}, notifiers={"ops-slack": fake_notifier}
    )

    run_task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0.05)
    await scheduler.stop()
    await run_task

    with db.session() as session:
        log = session.query(NotificationLog).one()
        assert log.success is False
        assert log.error_message == "webhook unreachable"


async def test_scheduler_skips_dispatch_when_no_notifiers_configured(db):
    with db.session() as session:
        session.add(_monitor(interval_seconds=60, failure_threshold=1, channels=["ops-slack"]))

    fake_checker = _FakeChecker(CheckOutcome(success=False, error_message="connection refused"))
    scheduler = Scheduler(db, checkers={MonitorType.HTTP: fake_checker})

    run_task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0.05)
    await scheduler.stop()
    await run_task

    with db.session() as session:
        assert session.query(Incident).one().state == IncidentState.OPEN
        assert session.query(NotificationLog).count() == 0


async def test_scheduler_skips_channel_with_no_matching_notifier(db):
    with db.session() as session:
        session.add(
            _monitor(interval_seconds=60, failure_threshold=1, channels=["ops-slack", "ghost"])
        )

    fake_checker = _FakeChecker(CheckOutcome(success=False, error_message="connection refused"))
    fake_notifier = _FakeNotifier()
    scheduler = Scheduler(
        db, checkers={MonitorType.HTTP: fake_checker}, notifiers={"ops-slack": fake_notifier}
    )

    run_task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0.05)
    await scheduler.stop()
    await run_task

    assert len(fake_notifier.messages_sent) == 1
    with db.session() as session:
        logs = session.query(NotificationLog).all()
        assert {log.channel_name for log in logs} == {"ops-slack"}


async def test_scheduler_does_not_resend_within_cooldown_on_later_checks(db):
    with db.session() as session:
        session.add(_monitor(interval_seconds=1, failure_threshold=1, channels=["ops-slack"]))

    fake_checker = _FakeChecker(CheckOutcome(success=False, error_message="connection refused"))
    fake_notifier = _FakeNotifier()
    scheduler = Scheduler(
        db,
        checkers={MonitorType.HTTP: fake_checker},
        notifiers={"ops-slack": fake_notifier},
        cooldown_seconds=3600,
    )

    run_task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(1.2)
    await scheduler.stop()
    await run_task

    assert fake_checker.calls >= 2
    assert len(fake_notifier.messages_sent) == 1
    with db.session() as session:
        assert session.query(NotificationLog).count() == 1


async def test_scheduler_stop_closes_notifiers(db):
    with db.session() as session:
        session.add(_monitor(interval_seconds=60))

    fake_checker = _FakeChecker(CheckOutcome(success=True, latency_ms=1.0, status_code=200))
    fake_notifier = _FakeNotifier()
    scheduler = Scheduler(
        db, checkers={MonitorType.HTTP: fake_checker}, notifiers={"ops-slack": fake_notifier}
    )

    run_task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0.05)
    await scheduler.stop()
    await run_task

    assert fake_notifier.closed is True


class _ExplodingChecker:
    """Raises what the checkers' own error handling never converts to an outcome."""

    def __init__(self):
        self.calls = 0

    async def check(self, *, target, timeout_seconds, expected_status):
        self.calls += 1
        raise RuntimeError("checker blew up")


class _DispatchingChecker:
    """Routes by target, so one monitor can explode while another passes."""

    def __init__(self, exploding_target: str):
        self._exploding_target = exploding_target
        self._exploding = _ExplodingChecker()
        self.healthy_calls = 0

    async def check(self, *, target, timeout_seconds, expected_status):
        if target == self._exploding_target:
            return await self._exploding.check(
                target=target, timeout_seconds=timeout_seconds, expected_status=expected_status
            )
        self.healthy_calls += 1
        return CheckOutcome(success=True, latency_ms=5.0, status_code=200)


async def test_monitor_loop_keeps_going_after_an_unexpected_checker_exception(db, caplog):
    """A raising checker must not end that monitor's loop."""
    with db.session() as session:
        session.add(_monitor(interval_seconds=1))

    checker = _ExplodingChecker()
    scheduler = Scheduler(db, checkers={MonitorType.HTTP: checker})

    run_task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(1.2)
    await scheduler.stop()
    await run_task

    assert checker.calls >= 2, "the loop checked again after the first exception"
    assert "check failed unexpectedly" in caplog.text


async def test_one_exploding_monitor_leaves_the_others_checking(db):
    """run() gathers every loop, so an escaping exception would stop them all."""
    with db.session() as session:
        session.add(_monitor(name="broken", target="https://broken.example.com", interval_seconds=1))
        session.add(_monitor(name="healthy", target="https://healthy.example.com", interval_seconds=1))

    checker = _DispatchingChecker(exploding_target="https://broken.example.com")
    scheduler = Scheduler(db, checkers={MonitorType.HTTP: checker})

    run_task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0.05)
    await scheduler.stop()
    await run_task

    assert checker.healthy_calls >= 1
    with db.session() as session:
        assert session.query(CheckResult).count() >= 1
