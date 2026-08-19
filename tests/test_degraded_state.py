"""Latency-budget driven DEGRADED state transitions.

Kept apart from test_scheduler.py, which covers the up/down state machine and
the check loop; these exercise only the `degraded_latency_ms` overlay on top
of it.
"""
from alerts.checkers.base import CheckOutcome
from alerts.config import MonitorConfig
from alerts.models import Monitor, MonitorState, MonitorType
from alerts.scheduler import apply_check_outcome, exceeds_latency_budget, sync_monitors


def _monitor(**overrides) -> Monitor:
    defaults = dict(
        name="site",
        type=MonitorType.HTTP,
        target="https://example.com",
        failure_threshold=2,
        recovery_threshold=2,
        degraded_latency_ms=500.0,
        state=MonitorState.UP,
        consecutive_failures=0,
        consecutive_successes=0,
        consecutive_fast_checks=0,
    )
    defaults.update(overrides)
    return Monitor(**defaults)


def _ok(latency_ms: float) -> CheckOutcome:
    return CheckOutcome(success=True, latency_ms=latency_ms, status_code=200)


def _fail() -> CheckOutcome:
    return CheckOutcome(success=False, error_message="boom")


def test_no_budget_configured_never_degrades():
    monitor = _monitor(degraded_latency_ms=None)

    apply_check_outcome(monitor, _ok(99_999.0))

    assert monitor.state == MonitorState.UP


def test_outcome_without_latency_is_not_degraded():
    """A checker that failed before connecting reports no latency to judge."""
    monitor = _monitor()

    assert exceeds_latency_budget(monitor, CheckOutcome(success=True)) is False


def test_latency_exactly_at_the_budget_counts_as_degraded():
    monitor = _monitor(degraded_latency_ms=500.0)

    assert exceeds_latency_budget(monitor, _ok(500.0)) is True
    assert exceeds_latency_budget(monitor, _ok(499.9)) is False


def test_single_slow_check_slides_up_into_degraded():
    monitor = _monitor(state=MonitorState.UP, consecutive_successes=10)

    apply_check_outcome(monitor, _ok(750.0))

    assert monitor.state == MonitorState.DEGRADED
    assert monitor.consecutive_successes == 11


def test_fast_checks_return_degraded_to_up_at_the_recovery_threshold():
    """A banked success streak from the slow stretch doesn't count as recovery."""
    monitor = _monitor(
        state=MonitorState.DEGRADED, consecutive_successes=5, recovery_threshold=2
    )

    apply_check_outcome(monitor, _ok(10.0))
    assert monitor.state == MonitorState.DEGRADED

    apply_check_outcome(monitor, _ok(10.0))
    assert monitor.state == MonitorState.UP


def test_degraded_stays_degraded_while_checks_stay_slow():
    monitor = _monitor(state=MonitorState.DEGRADED, consecutive_successes=1)

    apply_check_outcome(monitor, _ok(900.0))
    apply_check_outcome(monitor, _ok(900.0))

    assert monitor.state == MonitorState.DEGRADED


def test_degraded_monitor_still_goes_down_on_failures():
    monitor = _monitor(state=MonitorState.DEGRADED, failure_threshold=2)

    apply_check_outcome(monitor, _fail())
    assert monitor.state == MonitorState.DEGRADED

    apply_check_outcome(monitor, _fail())
    assert monitor.state == MonitorState.DOWN
    assert monitor.consecutive_successes == 0


def test_recovery_from_down_lands_on_degraded_when_the_check_is_slow():
    monitor = _monitor(state=MonitorState.DOWN, recovery_threshold=2)

    apply_check_outcome(monitor, _ok(800.0))
    assert monitor.state == MonitorState.DOWN, "one success is below recovery_threshold"

    apply_check_outcome(monitor, _ok(800.0))
    assert monitor.state == MonitorState.DEGRADED


def test_recovery_from_unknown_respects_the_recovery_threshold():
    monitor = _monitor(state=MonitorState.UNKNOWN, recovery_threshold=3)

    for _ in range(2):
        apply_check_outcome(monitor, _ok(10.0))
    assert monitor.state == MonitorState.UNKNOWN

    apply_check_outcome(monitor, _ok(10.0))
    assert monitor.state == MonitorState.UP


def test_sync_monitors_persists_the_budget_on_create(db):
    with db.session() as session:
        sync_monitors(
            session,
            [
                MonitorConfig(
                    name="site",
                    type="http",
                    target="https://example.com",
                    timeout_seconds=5,
                    degraded_latency_ms=1500,
                )
            ],
        )

    with db.session() as session:
        assert session.query(Monitor).one().degraded_latency_ms == 1500


def test_sync_monitors_clears_a_budget_removed_from_the_config(db):
    with db.session() as session:
        session.add(_monitor(degraded_latency_ms=1500.0))

    with db.session() as session:
        sync_monitors(
            session,
            [MonitorConfig(name="site", type="http", target="https://example.com")],
        )

    with db.session() as session:
        assert session.query(Monitor).one().degraded_latency_ms is None
