"""The scheduler holding back notifications during a maintenance window.

The scheduler asks about "now", so these use windows that are unambiguously
active or inactive whenever the suite runs: an all-day recurring window for
the active case, and a fixed window in the past for the inactive one.
"""
import asyncio

from alerts.checkers.base import CheckOutcome, CheckRequest
from alerts.config import MaintenanceWindowConfig
from alerts.models import (
    CheckResult,
    Incident,
    IncidentState,
    Monitor,
    MonitorState,
    MonitorType,
    NotificationLog,
)
from alerts.notifiers.base import NotifyResult
from alerts.scheduler import Scheduler

ALWAYS = MaintenanceWindowConfig(name="all-day", start="00:00", duration_minutes=24 * 60)
NEVER = MaintenanceWindowConfig(
    name="last-year", starts_at="2025-01-01T00:00:00Z", ends_at="2025-01-01T01:00:00Z"
)


def _monitor(**overrides) -> Monitor:
    defaults = dict(
        name="site",
        type=MonitorType.HTTP,
        target="https://example.com",
        interval_seconds=60,
        failure_threshold=1,
        recovery_threshold=1,
        channels=["ops-slack"],
        consecutive_failures=0,
        consecutive_successes=0,
        consecutive_fast_checks=0,
    )
    defaults.update(overrides)
    return Monitor(**defaults)


class _FakeChecker:
    def __init__(self, outcome: CheckOutcome):
        self.outcome = outcome

    async def check(self, request: CheckRequest) -> CheckOutcome:
        return self.outcome


class _FakeNotifier:
    def __init__(self):
        self.messages_sent = []

    async def send(self, message: str) -> NotifyResult:
        self.messages_sent.append(message)
        return NotifyResult(success=True)


async def _run_once(db, checker, notifier, windows):
    scheduler = Scheduler(
        db,
        checkers={MonitorType.HTTP: checker},
        notifiers={"ops-slack": notifier},
        maintenance_windows=windows,
    )
    run_task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0.05)
    await scheduler.stop()
    await run_task


async def test_no_notification_is_sent_during_a_window(db):
    with db.session() as session:
        session.add(_monitor())

    notifier = _FakeNotifier()
    await _run_once(
        db, _FakeChecker(CheckOutcome(success=False, error_message="boom")), notifier, [ALWAYS]
    )

    assert notifier.messages_sent == []


async def test_the_check_still_runs_and_is_recorded_during_a_window(db):
    """The window suppresses alerting, not monitoring."""
    with db.session() as session:
        session.add(_monitor())

    await _run_once(
        db,
        _FakeChecker(CheckOutcome(success=False, error_message="boom")),
        _FakeNotifier(),
        [ALWAYS],
    )

    with db.session() as session:
        result = session.query(CheckResult).one()
        assert result.success is False
        assert session.query(Monitor).one().state == MonitorState.DOWN


async def test_the_incident_is_still_opened_during_a_window(db):
    with db.session() as session:
        session.add(_monitor())

    await _run_once(
        db,
        _FakeChecker(CheckOutcome(success=False, error_message="boom")),
        _FakeNotifier(),
        [ALWAYS],
    )

    with db.session() as session:
        assert session.query(Incident).one().state == IncidentState.OPEN


async def test_nothing_is_written_to_the_notification_log_during_a_window(db):
    """So the incident is still announced if it outlives the window."""
    with db.session() as session:
        session.add(_monitor())

    await _run_once(
        db,
        _FakeChecker(CheckOutcome(success=False, error_message="boom")),
        _FakeNotifier(),
        [ALWAYS],
    )

    with db.session() as session:
        assert session.query(NotificationLog).count() == 0


async def test_an_expired_window_does_not_suppress(db):
    with db.session() as session:
        session.add(_monitor())

    notifier = _FakeNotifier()
    await _run_once(
        db, _FakeChecker(CheckOutcome(success=False, error_message="boom")), notifier, [NEVER]
    )

    assert len(notifier.messages_sent) == 1


async def test_a_window_scoped_to_another_monitor_does_not_suppress(db):
    with db.session() as session:
        session.add(_monitor(name="site"))

    scoped = MaintenanceWindowConfig(
        name="other-service", monitors=["something-else"], start="00:00", duration_minutes=1440
    )
    notifier = _FakeNotifier()
    await _run_once(
        db, _FakeChecker(CheckOutcome(success=False, error_message="boom")), notifier, [scoped]
    )

    assert len(notifier.messages_sent) == 1


async def test_a_window_naming_the_monitor_suppresses(db):
    with db.session() as session:
        session.add(_monitor(name="site"))

    scoped = MaintenanceWindowConfig(
        name="this-service", monitors=["site"], start="00:00", duration_minutes=1440
    )
    notifier = _FakeNotifier()
    await _run_once(
        db, _FakeChecker(CheckOutcome(success=False, error_message="boom")), notifier, [scoped]
    )

    assert notifier.messages_sent == []


async def test_no_windows_configured_notifies_as_before(db):
    with db.session() as session:
        session.add(_monitor())

    notifier = _FakeNotifier()
    await _run_once(
        db, _FakeChecker(CheckOutcome(success=False, error_message="boom")), notifier, []
    )

    assert len(notifier.messages_sent) == 1


async def test_an_incident_opened_in_a_window_is_announced_once_it_ends(db):
    """The suppressed check leaves no log entry, so the next one still pages."""
    with db.session() as session:
        session.add(_monitor())

    checker = _FakeChecker(CheckOutcome(success=False, error_message="boom"))
    notifier = _FakeNotifier()

    await _run_once(db, checker, notifier, [ALWAYS])
    assert notifier.messages_sent == []

    await _run_once(db, checker, notifier, [])

    assert len(notifier.messages_sent) == 1
    assert "[DOWN]" in notifier.messages_sent[0]


async def test_an_incident_that_opens_and_clears_inside_a_window_stays_quiet(db):
    """No "[RECOVERED]" for an outage the channel was never told about."""
    with db.session() as session:
        session.add(_monitor())

    notifier = _FakeNotifier()
    await _run_once(
        db, _FakeChecker(CheckOutcome(success=False, error_message="boom")), notifier, [ALWAYS]
    )
    await _run_once(
        db,
        _FakeChecker(CheckOutcome(success=True, latency_ms=5.0, status_code=200)),
        notifier,
        [ALWAYS],
    )

    with db.session() as session:
        assert session.query(Incident).one().state == IncidentState.RESOLVED

    notifier_after = _FakeNotifier()
    await _run_once(
        db,
        _FakeChecker(CheckOutcome(success=True, latency_ms=5.0, status_code=200)),
        notifier_after,
        [],
    )

    assert notifier.messages_sent == []
    assert notifier_after.messages_sent == [], "nothing to recover from, as far as the channel knows"
