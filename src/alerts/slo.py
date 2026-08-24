"""Error budget arithmetic for uptime service level objectives.

An availability SLO is a promise with a number on it: "99.9% of checks over
30 days succeed". The useful part is not the promise, it's the *budget* it
implies -- 0.1% of 30 days is 43 minutes of downtime you are allowed to
spend, and knowing how much of it is left is what turns "we had an outage"
into a decision about whether to ship the next risky change.

This module is the arithmetic only. It takes a target, a window, and a tally
of checks, and answers how much of the budget has been burned. It knows
nothing about the database, the config, or how any of it is rendered, so the
same numbers back `alerts slo`, the dashboard, the JSON API, and the metrics
exposition without any of them reimplementing the division.

Availability is measured in checks rather than in seconds. The scheduler
records one row per check, and a check is the only direct evidence this tool
has about whether a service was serving; converting to a duration means
assuming each check speaks for the whole interval around it. The duration
figures below make exactly that assumption, and say so, because "23 minutes
of budget left" is the sentence people actually reason with -- but the check
counts are the measurement and the durations are derived from them.
"""
from __future__ import annotations

import enum
import math
from dataclasses import dataclass
from typing import Dict, Iterable, Optional

# The bounds a target has to fall in to mean anything. 0 is excluded because
# a service that is allowed to be down always has no objective; 100 is
# allowed, and means a budget of zero -- see `allowance`.
MIN_TARGET = 0.0
MAX_TARGET = 100.0

# The default rolling window. Thirty days is the near-universal convention
# for availability SLOs, and it is long enough that a single bad afternoon
# doesn't swing the figure while still being short enough to act on.
DEFAULT_WINDOW_DAYS = 30

# How much of the budget has to be gone before a monitor is called at risk.
# Three quarters, so a report warns while there is still a quarter of the
# month's allowance left to spend deliberately, rather than announcing the
# breach after it has happened.
AT_RISK_RATIO = 0.75

SECONDS_PER_DAY = 86_400

# Decimal places the failure ratio is rounded to, to keep binary floating
# point noise out of every figure derived from it. See `failure_ratio`.
_RATIO_PLACES = 12


class BudgetStatus(str, enum.Enum):
    """Where a monitor stands against its objective.

    NO_DATA is its own status rather than an optimistic OK. A monitor that
    has never been checked has not met its objective; it has failed to
    produce any evidence either way, and reporting that as compliance is the
    one answer that could let an unmonitored service look healthy.
    """

    OK = "ok"
    AT_RISK = "at-risk"
    BREACHED = "breached"
    NO_DATA = "no-data"


def is_valid_target(target: float) -> bool:
    """Whether a percentage can serve as an availability target."""
    return MIN_TARGET < target <= MAX_TARGET


@dataclass(frozen=True)
class ErrorBudget:
    """How much of one monitor's error budget a window of checks has burned.

    Built from a tally rather than from the check rows themselves: the
    counts are two `COUNT(*)` queries over an indexed column, and loading a
    month of check results per monitor to length-check them would be a large
    read for a small number.

    Every derived figure is a property rather than a stored field, so an
    instance can't be constructed into a state where its own numbers
    disagree with each other.
    """

    target_percentage: float
    window_days: int
    total_checks: int
    failed_checks: int

    def __post_init__(self) -> None:
        if not is_valid_target(self.target_percentage):
            raise ValueError(
                f"SLO target {self.target_percentage} must be above {MIN_TARGET} "
                f"and at most {MAX_TARGET}"
            )
        if self.window_days < 1:
            raise ValueError(f"SLO window {self.window_days} must be at least one day")
        if self.total_checks < 0 or self.failed_checks < 0:
            raise ValueError("check counts cannot be negative")
        if self.failed_checks > self.total_checks:
            raise ValueError(
                f"{self.failed_checks} failed checks out of {self.total_checks} total"
            )

    @property
    def has_data(self) -> bool:
        return self.total_checks > 0

    @property
    def successful_checks(self) -> int:
        return self.total_checks - self.failed_checks

    @property
    def actual_percentage(self) -> Optional[float]:
        """Measured availability over the window, or None with nothing to measure.

        None rather than 0 for an empty window, matching how `UptimeStats`
        reports an absence: a monitor with no checks was not 0% available,
        it was not observed.
        """
        if not self.has_data:
            return None
        return (self.successful_checks / self.total_checks) * 100

    @property
    def failure_ratio(self) -> float:
        """The fraction of the target that is spent on being allowed to fail.

        A 99.9% target has a failure ratio of 0.001. This is the number the
        whole module turns on, so it is named rather than written out at
        each of the four places that need it.

        Rounded, because binary floating point cannot hold 99.9 and the
        subtraction below magnifies the gap: the unrounded complement of
        99.9% is 0.0010000000000000009, which turns a monthly allowance of
        exactly 43m12s into 2591.9999999998527 seconds. The table format
        hides that; the JSON payload and the metrics exposition do not, and
        a status page rendering a budget nobody can reconcile with the
        arithmetic they did on paper is worse than one showing no budget.

        Twelve places is far more precision than any target carries -- the
        tightest anyone writes is five nines -- and far less than the noise
        it removes.
        """
        return round((MAX_TARGET - self.target_percentage) / MAX_TARGET, _RATIO_PLACES)

    @property
    def allowance(self) -> float:
        """How many failed checks the window's length affords, unrounded.

        Deliberately fractional. A 99.9% target over 500 checks affords half
        a failure, and rounding that up to one would quietly hand out a
        budget the objective never promised -- one failure in 500 is 99.8%,
        which is a breach whatever the rounding says. `consumed_ratio`
        divides by this, so the ratio and the availability figure can never
        disagree about whether the objective is being met.

        Zero for a 100% target, and zero for an empty window.
        """
        return self.total_checks * self.failure_ratio

    @property
    def allowed_failures(self) -> int:
        """Whole failed checks the window affords, for reporting.

        Floored, because half a failure is not a failure anyone can spend:
        an allowance of 0.5 means the next failure is a breach, and saying
        "0 left" is the honest way to put that. `consumed_ratio` uses the
        unrounded `allowance` instead, so nothing downstream inherits the
        rounding.
        """
        return math.floor(self.allowance)

    @property
    def remaining_failures(self) -> int:
        """Failed checks still affordable before the objective is missed."""
        return max(self.allowed_failures - self.failed_checks, 0)

    @property
    def consumed_ratio(self) -> Optional[float]:
        """The fraction of the budget already spent, or None without data.

        Above 1 exactly when the objective is being missed, which is what
        makes it safe to drive both the status below and a Prometheus alert
        rule from one number.

        A 100% target has no budget to spend, so the ratio is 0 while
        nothing has failed and 1 the moment something has -- there is no
        meaningful fraction between "perfect" and "broken" when the
        allowance is zero, and dividing by it would be an error rather than
        an answer.
        """
        if not self.has_data:
            return None
        if self.allowance <= 0:
            return 1.0 if self.failed_checks else 0.0
        return self.failed_checks / self.allowance

    @property
    def remaining_ratio(self) -> Optional[float]:
        """The fraction of the budget still unspent, clipped at zero.

        Clipped because a budget can be overspent -- `consumed_ratio` of 3.0
        is a real and useful reading -- while "minus 200% remaining" is not
        a sentence any report should print.
        """
        consumed = self.consumed_ratio
        if consumed is None:
            return None
        return max(1.0 - consumed, 0.0)

    @property
    def is_breaching(self) -> bool:
        """Whether measured availability has fallen below the target."""
        actual = self.actual_percentage
        return actual is not None and actual < self.target_percentage

    @property
    def is_at_risk(self) -> bool:
        """Whether most of the budget is gone but the objective still holds."""
        consumed = self.consumed_ratio
        return (
            consumed is not None and not self.is_breaching and consumed >= AT_RISK_RATIO
        )

    @property
    def status(self) -> BudgetStatus:
        if not self.has_data:
            return BudgetStatus.NO_DATA
        if self.is_breaching:
            return BudgetStatus.BREACHED
        if self.is_at_risk:
            return BudgetStatus.AT_RISK
        return BudgetStatus.OK

    @property
    def window_seconds(self) -> int:
        return self.window_days * SECONDS_PER_DAY

    @property
    def allowed_downtime_seconds(self) -> float:
        """Downtime the objective affords over the whole window.

        The figure an SLO is usually quoted by -- 99.9% over 30 days is 43
        minutes -- and the one that makes a target comprehensible before any
        checks have run, since it depends on the promise alone.
        """
        return self.window_seconds * self.failure_ratio

    @property
    def observed_downtime_seconds(self) -> Optional[float]:
        """Downtime implied by the failed checks, spread over the window.

        An estimate, and the one place this module leaves counting. Each
        check is taken to speak for an equal share of the window, so a
        monitor that failed 1% of its checks is reported as having been down
        for 1% of the window. That over-reports an outage caught by a single
        check on a long interval and under-reports one that ended just after
        a check passed.

        It is still worth reporting, because the alternative is asking
        someone to convert "17 failed checks" into minutes in their head
        using an interval they would have to go and look up. The check
        counts sit next to it in every report for exactly that reason.
        """
        if not self.has_data:
            return None
        return (self.failed_checks / self.total_checks) * self.window_seconds

    @property
    def remaining_downtime_seconds(self) -> Optional[float]:
        """Downtime still affordable this window, clipped at zero."""
        observed = self.observed_downtime_seconds
        if observed is None:
            return None
        return max(self.allowed_downtime_seconds - observed, 0.0)


def compute_error_budget(
    *,
    target_percentage: float,
    total_checks: int,
    failed_checks: int,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> ErrorBudget:
    """Build an `ErrorBudget` from a target and a tally of checks.

    A thin constructor, kept so callers read as a sentence about a monitor
    rather than as positional arguments in an order they have to remember,
    and so the tally can grow a field later without every call site moving.
    """
    return ErrorBudget(
        target_percentage=target_percentage,
        window_days=window_days,
        total_checks=total_checks,
        failed_checks=failed_checks,
    )


@dataclass(frozen=True)
class BudgetSummary:
    """How many monitors sit in each budget status right now.

    Every `BudgetStatus` is present in `counts`, zeroes included, for the
    same reason `StateSummary` carries every state: a caller rendering a
    fixed set of tiles shouldn't have to decide what to do about a status
    nothing happens to be in.

    `total` counts only the monitors that have an objective. Folding in the
    ones that don't would make "2 of 40 breaching" read as reassurance about
    38 services nobody has promised anything about.
    """

    total: int
    counts: Dict[BudgetStatus, int]

    @property
    def breaching(self) -> int:
        return self.counts[BudgetStatus.BREACHED]

    @property
    def at_risk(self) -> int:
        return self.counts[BudgetStatus.AT_RISK]

    @property
    def all_met(self) -> bool:
        """Whether every objective is currently being met.

        False when nothing has an objective at all: there is no promise
        being kept, and answering "yes, all met" about an estate that has
        made no promises is the reading most likely to be misread.
        """
        return self.total > 0 and self.breaching == 0


def summarize_budgets(budgets: Iterable[Optional[ErrorBudget]]) -> BudgetSummary:
    """Tally a collection of budgets by status, skipping the monitors without one.

    Takes `None`s in its stride so a caller can map straight over its
    monitors without filtering first -- which is what every caller has,
    since "has no objective" is a per-monitor fact rather than a separate
    list.
    """
    counts = {status: 0 for status in BudgetStatus}
    total = 0
    for budget in budgets:
        if budget is None:
            continue
        counts[budget.status] += 1
        total += 1
    return BudgetSummary(total=total, counts=counts)
