"""The scheduler holding back a page while something upstream is down.

The estate is the smallest one that shows the point: a `web` monitor that
depends on `db`. `web` fails every check in every test here -- what
changes is what `db` is doing, and therefore whether anyone is told about
`web`.

Suppression must not touch monitoring: the check still runs, the result
and the incident are still recorded, and nothing is written to the
notification log, so the alert goes out as soon as the upstream recovers
if `web` is still broken then.
"""
import asyncio

from alerts.checkers.base import CheckOutcome, CheckRequest
from alerts.dependencies import build_graph
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

WEB_NEEDS_DB = build_graph([("web", ["db"])])
WEB_NEEDS_API_NEEDS_DB = build_graph([("web", ["api"]), ("api", ["db"])])


def _monitor(name="web", state=MonitorState.UNKNOWN, **overrides) -> Monitor:
    defaults = dict(
        name=name,
        type=MonitorType.HTTP,
        target="https://example.com",
        interval_seconds=60,
        failure_threshold=1,
        recovery_threshold=1,
        channels=["ops-slack"],
        state=state,
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


async def _run_once(db, checker, notifier, dependencies):
    scheduler = Scheduler(
        db,
        checkers={MonitorType.HTTP: checker, MonitorType.TCP: checker},
        notifiers={"ops-slack": notifier},
        dependencies=dependencies,
    )
    run_task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0.05)
    await scheduler.stop()
    await run_task


def _failing() -> _FakeChecker:
    return _FakeChecker(CheckOutcome(success=False, error_message="boom"))


def _web_only(db, db_state=MonitorState.DOWN, upstream_name="db", **upstream):
    """`web`, plus the upstream it depends on sitting at a given state.

    The upstream carries no channels, so the scheduler checking it (which
    it does, and should) can't put a notification of its own in front of
    the assertions below -- every message a test sees is one about `web`.
    """
    with db.session() as session:
        session.add(_monitor("web"))
        session.add(
            Monitor(
                name=upstream_name,
                type=MonitorType.TCP,
                target="db.example.com:5432",
                state=db_state,
                channels=[],
                **upstream,
            )
        )


async def test_no_page_while_the_upstream_is_down(db):
    _web_only(db, MonitorState.DOWN)

    notifier = _FakeNotifier()
    await _run_once(db, _failing(), notifier, WEB_NEEDS_DB)

    assert notifier.messages_sent == []


async def test_the_page_goes_out_when_the_upstream_is_up(db):
    _web_only(db, MonitorState.UP)

    notifier = _FakeNotifier()
    await _run_once(db, _failing(), notifier, WEB_NEEDS_DB)

    assert len(notifier.messages_sent) == 1


async def test_a_degraded_upstream_does_not_suppress(db):
    """Degraded is still serving; a dependent failing under it is its own outage."""
    _web_only(db, MonitorState.DEGRADED)

    notifier = _FakeNotifier()
    await _run_once(db, _failing(), notifier, WEB_NEEDS_DB)

    assert len(notifier.messages_sent) == 1


async def test_an_unknown_upstream_does_not_suppress(db):
    # A monitor nobody has checked yet has produced no evidence that
    # anything is wrong with it, and silence based on no evidence is the
    # failure mode this whole project keeps refusing.
    _web_only(db, MonitorState.UNKNOWN)

    notifier = _FakeNotifier()
    await _run_once(db, _failing(), notifier, WEB_NEEDS_DB)

    assert len(notifier.messages_sent) == 1


async def test_a_paused_upstream_does_not_suppress(db):
    """Its DOWN reading is frozen, so it would suppress `web` forever."""
    _web_only(db, MonitorState.DOWN, enabled=False)

    notifier = _FakeNotifier()
    await _run_once(db, _failing(), notifier, WEB_NEEDS_DB)

    assert len(notifier.messages_sent) == 1


async def test_a_monitor_declaring_no_dependencies_pages_as_before(db):
    _web_only(db, MonitorState.DOWN)

    notifier = _FakeNotifier()
    await _run_once(db, _failing(), notifier, build_graph([]))

    assert len(notifier.messages_sent) == 1


async def test_a_dependency_on_a_healthy_monitor_pages_as_before(db):
    _web_only(db, MonitorState.DOWN, upstream_name="cache")

    notifier = _FakeNotifier()
    await _run_once(db, _failing(), notifier, build_graph([("web", ["something-else"])]))

    assert len(notifier.messages_sent) == 1


async def test_a_transitive_upstream_suppresses_too(db):
    """The intermediate is still UP -- which is normal, for a minute or two."""
    with db.session() as session:
        session.add(_monitor("web"))
        session.add(
            Monitor(name="api", type=MonitorType.TCP, target="api:80", state=MonitorState.UP)
        )
        session.add(
            Monitor(name="db", type=MonitorType.TCP, target="db:5432", state=MonitorState.DOWN)
        )

    notifier = _FakeNotifier()
    await _run_once(db, _failing(), notifier, WEB_NEEDS_API_NEEDS_DB)

    assert notifier.messages_sent == []


async def test_the_check_still_runs_and_is_recorded_while_blocked(db):
    """Suppression is about paging, not about monitoring."""
    _web_only(db, MonitorState.DOWN)

    await _run_once(db, _failing(), _FakeNotifier(), WEB_NEEDS_DB)

    with db.session() as session:
        monitor = session.query(Monitor).filter_by(name="web").one()
        result = session.query(CheckResult).filter_by(monitor_id=monitor.id).one()
        assert result.success is False
        assert result.error_message == "boom"
        assert monitor.state == MonitorState.DOWN


async def test_the_incident_is_still_opened_while_blocked(db):
    _web_only(db, MonitorState.DOWN)

    await _run_once(db, _failing(), _FakeNotifier(), WEB_NEEDS_DB)

    with db.session() as session:
        monitor = session.query(Monitor).filter_by(name="web").one()
        incident = session.query(Incident).filter_by(monitor_id=monitor.id).one()
        assert incident.state == IncidentState.OPEN


async def test_nothing_is_written_to_the_notification_log_while_blocked(db):
    _web_only(db, MonitorState.DOWN)

    await _run_once(db, _failing(), _FakeNotifier(), WEB_NEEDS_DB)

    with db.session() as session:
        assert session.query(NotificationLog).count() == 0


async def test_the_page_goes_out_once_the_upstream_recovers(db):
    """Nothing was logged as sent, so the still-broken dependent pages then."""
    _web_only(db, MonitorState.DOWN)

    notifier = _FakeNotifier()
    await _run_once(db, _failing(), notifier, WEB_NEEDS_DB)
    assert notifier.messages_sent == []

    with db.session() as session:
        session.query(Monitor).filter_by(name="db").one().state = MonitorState.UP

    await _run_once(db, _failing(), notifier, WEB_NEEDS_DB)
    assert len(notifier.messages_sent) == 1
