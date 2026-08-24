"""Error budget arithmetic.

The interesting cases are all at the edges: a window with nothing in it, a
target of exactly 100%, an allowance that floors to zero, and a budget that
has been overspent. Those are where a plausible-looking implementation
quietly reports a service as compliant when it isn't, so they get pinned
one at a time rather than through a couple of happy-path totals.
"""
import pytest

from alerts.slo import (
    AT_RISK_RATIO,
    DEFAULT_WINDOW_DAYS,
    SECONDS_PER_DAY,
    BudgetStatus,
    ErrorBudget,
    compute_error_budget,
    is_valid_target,
)


def _budget(target=99.9, total=1000, failed=0, window_days=DEFAULT_WINDOW_DAYS):
    return compute_error_budget(
        target_percentage=target,
        total_checks=total,
        failed_checks=failed,
        window_days=window_days,
    )


def test_a_target_must_be_above_zero_and_at_most_a_hundred():
    assert is_valid_target(99.9)
    assert is_valid_target(100.0)
    assert not is_valid_target(0.0)
    assert not is_valid_target(-1.0)
    assert not is_valid_target(100.1)


def test_an_out_of_range_target_is_refused_at_construction():
    with pytest.raises(ValueError, match="must be above"):
        _budget(target=0.0)
    with pytest.raises(ValueError, match="must be above"):
        _budget(target=101.0)


def test_a_window_shorter_than_a_day_is_refused():
    with pytest.raises(ValueError, match="at least one day"):
        _budget(window_days=0)


def test_negative_counts_are_refused():
    with pytest.raises(ValueError, match="cannot be negative"):
        _budget(total=-1)
    with pytest.raises(ValueError, match="cannot be negative"):
        _budget(failed=-1)


def test_more_failures_than_checks_is_refused():
    """A tally that can't have come from a real window is a bug upstream."""
    with pytest.raises(ValueError, match="out of"):
        _budget(total=10, failed=11)


def test_an_empty_window_reports_no_data_rather_than_zero_percent():
    """A monitor nobody checked was not 0% available, it was not observed."""
    budget = _budget(total=0, failed=0)

    assert budget.has_data is False
    assert budget.actual_percentage is None
    assert budget.consumed_ratio is None
    assert budget.remaining_ratio is None
    assert budget.observed_downtime_seconds is None
    assert budget.remaining_downtime_seconds is None
    assert budget.status is BudgetStatus.NO_DATA


def test_an_empty_window_is_not_reported_as_breaching():
    assert _budget(total=0).is_breaching is False
    assert _budget(total=0).is_at_risk is False


def test_availability_is_the_share_of_checks_that_passed():
    budget = _budget(total=1000, failed=1)

    assert budget.successful_checks == 999
    assert budget.actual_percentage == pytest.approx(99.9)


def test_the_failure_ratio_is_the_complement_of_the_target():
    """Exactly, not approximately -- see the rounding on `failure_ratio`.

    Binary floating point cannot hold 99.9, so the raw complement is
    0.0010000000000000009 and every figure derived from it inherits the
    noise: a monthly allowance of exactly 43m12s renders as
    2591.9999999998527 in the JSON payload and the metrics exposition. A
    budget nobody can reconcile with the arithmetic they did on paper is
    worse than no budget at all.
    """
    assert _budget(target=99.9).failure_ratio == 0.001
    assert _budget(target=99.99).failure_ratio == 0.0001
    assert _budget(target=99.0).failure_ratio == 0.01
    assert _budget(target=100.0).failure_ratio == 0.0


def test_the_derived_durations_come_out_exact():
    assert _budget(target=99.9, window_days=30).allowed_downtime_seconds == 2592.0
    assert _budget(target=99.99, window_days=30).allowed_downtime_seconds == 259.2


def test_the_allowance_is_the_failure_ratio_over_the_window_s_checks():
    assert _budget(target=99.9, total=2000).allowance == pytest.approx(2.0)
    assert _budget(target=99.0, total=2000).allowance == pytest.approx(20.0)


def test_the_allowance_stays_fractional_and_the_reported_count_floors_it():
    """Half a failure is real budget, but not a failure anyone can spend.

    Rounding the allowance up would hand out a failure the objective never
    promised: one failure in 500 checks is 99.8%, a breach of a 99.9%
    target whatever the reported count says.
    """
    budget = _budget(target=99.9, total=500)

    assert budget.allowance == pytest.approx(0.5)
    assert budget.allowed_failures == 0
    assert budget.remaining_failures == 0


def test_a_window_too_short_to_afford_a_failure_breaches_on_the_first_one():
    budget = _budget(target=99.9, total=500, failed=1)

    assert budget.is_breaching is True
    assert budget.consumed_ratio == pytest.approx(2.0)


def test_remaining_failures_never_goes_negative():
    budget = _budget(target=99.0, total=1000, failed=50)

    assert budget.allowed_failures == 10
    assert budget.remaining_failures == 0


def test_a_spotless_window_has_burned_nothing():
    budget = _budget(total=1000, failed=0)

    assert budget.consumed_ratio == 0.0
    assert budget.remaining_ratio == 1.0
    assert budget.status is BudgetStatus.OK


def test_consumed_ratio_crosses_one_exactly_when_the_target_is_missed():
    """The property that lets one number drive both a report and an alert."""
    on_target = _budget(target=99.0, total=1000, failed=10)
    missed = _budget(target=99.0, total=1000, failed=11)

    assert on_target.consumed_ratio == pytest.approx(1.0)
    assert on_target.is_breaching is False
    assert missed.consumed_ratio > 1.0
    assert missed.is_breaching is True


def test_an_overspent_budget_reports_a_ratio_above_one():
    budget = _budget(target=99.0, total=1000, failed=30)

    assert budget.consumed_ratio == pytest.approx(3.0)
    assert budget.remaining_ratio == 0.0


def test_a_budget_three_quarters_gone_is_at_risk():
    """Ten affordable failures, eight of them spent."""
    budget = _budget(target=99.0, total=1000, failed=8)

    assert budget.allowed_failures == 10
    assert budget.consumed_ratio == pytest.approx(0.8)
    assert budget.consumed_ratio >= AT_RISK_RATIO
    assert budget.is_at_risk is True
    assert budget.status is BudgetStatus.AT_RISK


def test_a_breaching_budget_is_not_also_reported_as_at_risk():
    """The two statuses are exclusive, so a report picks one word."""
    budget = _budget(target=99.0, total=1000, failed=200)

    assert budget.is_breaching is True
    assert budget.is_at_risk is False
    assert budget.status is BudgetStatus.BREACHED


def test_a_perfect_target_has_no_budget_to_spend():
    """100% affords nothing, so the ratio is 0 or 1 with nothing between."""
    spotless = _budget(target=100.0, total=100, failed=0)
    broken = _budget(target=100.0, total=100, failed=1)

    assert spotless.allowance == 0.0
    assert spotless.consumed_ratio == 0.0
    assert spotless.status is BudgetStatus.OK
    assert broken.consumed_ratio == 1.0
    assert broken.is_breaching is True
    assert broken.status is BudgetStatus.BREACHED


def test_allowed_downtime_depends_on_the_promise_alone():
    """43 minutes a month is the figure a 99.9% target is quoted by."""
    budget = _budget(target=99.9, window_days=30, total=0)

    assert budget.window_seconds == 30 * SECONDS_PER_DAY
    assert budget.allowed_downtime_seconds == pytest.approx(2592.0)


def test_observed_downtime_spreads_the_failed_checks_over_the_window():
    budget = _budget(target=99.9, window_days=30, total=1000, failed=10)

    assert budget.observed_downtime_seconds == pytest.approx(0.01 * 30 * SECONDS_PER_DAY)


def test_remaining_downtime_is_what_the_promise_affords_less_what_was_spent():
    budget = _budget(target=99.0, window_days=30, total=1000, failed=5)

    expected = budget.allowed_downtime_seconds - budget.observed_downtime_seconds
    assert budget.remaining_downtime_seconds == pytest.approx(expected)


def test_remaining_downtime_never_goes_negative():
    budget = _budget(target=99.9, window_days=30, total=1000, failed=500)

    assert budget.remaining_downtime_seconds == 0.0


def test_the_window_length_does_not_move_the_availability_figure():
    """Availability is measured in checks; the window only scales durations."""
    short = _budget(total=1000, failed=1, window_days=1)
    long = _budget(total=1000, failed=1, window_days=90)

    assert short.actual_percentage == long.actual_percentage
    assert short.consumed_ratio == long.consumed_ratio
    assert long.allowed_downtime_seconds == pytest.approx(
        90 * short.allowed_downtime_seconds
    )


def test_the_default_window_is_thirty_days():
    assert DEFAULT_WINDOW_DAYS == 30
    assert compute_error_budget(
        target_percentage=99.9, total_checks=1, failed_checks=0
    ).window_days == 30


def test_the_budget_is_frozen():
    """Nothing downstream can edit a tally out from under its own figures."""
    budget = _budget()

    with pytest.raises(Exception):
        budget.failed_checks = 5  # type: ignore[misc]


def test_the_dataclass_can_be_built_directly():
    """The keyword constructor is a convenience, not the only way in."""
    budget = ErrorBudget(
        target_percentage=99.5, window_days=7, total_checks=1000, failed_checks=1
    )

    assert budget.actual_percentage == pytest.approx(99.9)
    assert budget.status is BudgetStatus.OK
