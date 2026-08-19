"""Decides whether a monitor is inside a planned maintenance window.

Windows suppress notifications, not checks: the scheduler keeps checking
and keeps writing check results and incidents throughout, so the history of
what a deploy did to a service survives the window. Only the decision to
page someone is held back.

All window times are UTC. That's a deliberate simplification over local
time: a recurring 02:00 window that silently moves an hour twice a year is
a worse surprise than one that stays where it was put, and every timestamp
this project stores is UTC already.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, List, Optional, Sequence

from alerts.config import WEEKDAYS, MaintenanceWindowConfig
from alerts.timeutil import as_utc_naive, utcnow


@dataclass(frozen=True)
class WindowOccurrence:
    """One concrete span a window covers, resolved against a real date."""

    name: str
    starts_at: datetime
    ends_at: datetime

    def covers(self, moment: datetime) -> bool:
        """Whether a moment falls inside this occurrence.

        Half-open: the start is included, the end is not. A window ending at
        03:00 and another starting at 03:00 shouldn't both claim that
        instant, and a zero-length span shouldn't cover anything.
        """
        return self.starts_at <= as_utc_naive(moment) < self.ends_at

    @property
    def duration(self) -> timedelta:
        return self.ends_at - self.starts_at


def _weekday_name(moment: datetime) -> str:
    return WEEKDAYS[moment.weekday()]


def _recurring_occurrences(
    window: MaintenanceWindowConfig, moment: datetime
) -> List[WindowOccurrence]:
    """The occurrences of a recurring window that could contain `moment`.

    Two candidates are considered: the one starting on `moment`'s own date
    and the one starting the day before. The second is what makes a window
    running past midnight work -- 23:00 for two hours is still active at
    00:30, and that occurrence belongs to the previous day, which is also
    the day its `days` filter is matched against.
    """
    assert window.start is not None and window.duration_minutes is not None
    duration = timedelta(minutes=window.duration_minutes)
    occurrences = []
    for day_offset in (0, -1):
        start_date = (moment + timedelta(days=day_offset)).date()
        starts_at = datetime.combine(start_date, window.start)
        if window.days and _weekday_name(starts_at) not in window.days:
            continue
        occurrences.append(
            WindowOccurrence(
                name=window.name, starts_at=starts_at, ends_at=starts_at + duration
            )
        )
    return occurrences


def _fixed_occurrence(window: MaintenanceWindowConfig) -> WindowOccurrence:
    assert window.starts_at is not None and window.ends_at is not None
    return WindowOccurrence(
        name=window.name,
        starts_at=as_utc_naive(window.starts_at),
        ends_at=as_utc_naive(window.ends_at),
    )


def occurrences_around(
    window: MaintenanceWindowConfig, moment: datetime
) -> List[WindowOccurrence]:
    """Every concrete span of `window` that could contain `moment`."""
    if window.is_recurring:
        return _recurring_occurrences(window, as_utc_naive(moment))
    return [_fixed_occurrence(window)]


def active_occurrence(
    window: MaintenanceWindowConfig, monitor_name: str, moment: datetime
) -> Optional[WindowOccurrence]:
    """The occurrence of `window` covering `monitor_name` right now, if any."""
    if not window.applies_to(monitor_name):
        return None
    for occurrence in occurrences_around(window, moment):
        if occurrence.covers(moment):
            return occurrence
    return None


def active_windows(
    windows: Iterable[MaintenanceWindowConfig],
    monitor_name: str,
    *,
    now: Optional[datetime] = None,
) -> List[WindowOccurrence]:
    """Every window currently suppressing notifications for a monitor.

    Returns all of them rather than the first match: overlapping windows are
    normal (an estate-wide freeze plus a service's own change window), and a
    caller reporting *why* a monitor is quiet should be able to name each.
    """
    moment = now or utcnow()
    found = []
    for window in windows:
        occurrence = active_occurrence(window, monitor_name, moment)
        if occurrence is not None:
            found.append(occurrence)
    return found


def next_occurrence(
    window: MaintenanceWindowConfig, *, now: Optional[datetime] = None
) -> Optional[WindowOccurrence]:
    """The next occurrence of `window` that starts after `now`.

    None once a fixed window has started or passed, and None for a recurring
    window whose `days` filter is somehow unsatisfiable. Recurring windows
    are searched a week and a day ahead, which is enough for any weekday
    filter to come round: the extra day covers a window whose start time has
    already passed today.

    An occurrence currently running is not "next" -- callers report that
    separately, since "active now" and "starts in 3 hours" answer different
    questions.
    """
    moment = as_utc_naive(now or utcnow())
    if not window.is_recurring:
        occurrence = _fixed_occurrence(window)
        return occurrence if occurrence.starts_at > moment else None

    assert window.start is not None and window.duration_minutes is not None
    duration = timedelta(minutes=window.duration_minutes)
    for day_offset in range(0, 8):
        start_date = (moment + timedelta(days=day_offset)).date()
        starts_at = datetime.combine(start_date, window.start)
        if starts_at <= moment:
            continue
        if window.days and _weekday_name(starts_at) not in window.days:
            continue
        return WindowOccurrence(
            name=window.name, starts_at=starts_at, ends_at=starts_at + duration
        )
    return None


def describe_schedule(window: MaintenanceWindowConfig) -> str:
    """A one-line, human-readable rendering of when a window applies."""
    if not window.is_recurring:
        assert window.starts_at is not None and window.ends_at is not None
        return (
            f"{as_utc_naive(window.starts_at).isoformat(timespec='minutes')} to "
            f"{as_utc_naive(window.ends_at).isoformat(timespec='minutes')} UTC"
        )
    assert window.start is not None and window.duration_minutes is not None
    days = ",".join(window.days) if window.days else "daily"
    return f"{days} at {window.start.strftime('%H:%M')} UTC for {window.duration_minutes}m"


def is_in_maintenance(
    windows: Sequence[MaintenanceWindowConfig],
    monitor_name: str,
    *,
    now: Optional[datetime] = None,
) -> bool:
    """Whether any configured window is suppressing this monitor right now."""
    return bool(active_windows(windows, monitor_name, now=now))
