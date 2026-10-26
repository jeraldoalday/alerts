"""UTC time helpers shared by the models, incident lifecycle, and notifications.

Everything this project stores is UTC, but it doesn't round-trip that way:
values are written timezone-aware and SQLite's `DateTime` column drops the
tzinfo on read, so a freshly computed "now" and a value loaded from the
database can't be compared or subtracted directly. `as_utc_naive` converts
whichever side still carries tzinfo so the two are comparable again -- and
converts rather than merely discards, because a config file may legitimately
name a moment in some other offset.

The same normalisation matters for query parameters, not just Python-side
arithmetic: SQLite stores datetimes as ISO strings, so binding an aware
datetime ("...+00:00") against a column holding naive ones compares strings
with a trailing offset against strings without it, and rows silently fall on
the wrong side of the cutoff.
"""
from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """The current time, timezone-aware in UTC."""
    return datetime.now(timezone.utc)


def as_utc_naive(value: datetime) -> datetime:
    """Convert an aware datetime to UTC and drop its tzinfo; leave a naive one alone.

    The conversion is the part that matters, and it is not free. Most values
    reaching here are already UTC -- `utcnow()` produces them and every
    column stores them -- for which converting is a no-op and simply
    dropping the tzinfo would have been equivalent.

    Not every value is, though. `starts_at: 2026-09-01T01:00:00+02:00` in a
    maintenance window is valid YAML and pydantic keeps the offset, so
    discarding tzinfo would file 01:00 in Berlin as 01:00 UTC: the window
    would suppress nothing during the two hours of the change it was written
    for, and go on suppressing alerts for two hours after it ended. Nothing
    about that is visible in the config, in the schedule the report prints,
    or in the fact that alerting simply didn't happen when it should have.

    A naive value is returned untouched. It is already the local convention
    of this codebase -- UTC without the label -- and there is nothing to
    convert from.
    """
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def format_duration(seconds: float) -> str:
    """A compact, human-readable duration ("3d 4h", "12m").

    Lives alongside the other time helpers rather than with the reporting
    tables it was first written for, so a notification message can render
    one without importing the reporting module's much larger dependency
    chain (which reaches back into the scheduler) just for this.
    """
    seconds = max(int(seconds), 0)
    days, seconds = divmod(seconds, 86_400)
    hours, seconds = divmod(seconds, 3_600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"
