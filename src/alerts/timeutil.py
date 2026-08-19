"""UTC time helpers shared by the models, incident lifecycle, and notifications.

Everything this project stores is UTC, but it doesn't round-trip that way:
values are written timezone-aware and SQLite's `DateTime` column drops the
tzinfo on read, so a freshly computed "now" and a value loaded from the
database can't be compared or subtracted directly. `as_utc_naive` normalises
whichever side still carries tzinfo so the two are comparable again.

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
    """Drop tzinfo from an aware UTC datetime, leaving a naive one untouched."""
    return value.replace(tzinfo=None) if value.tzinfo is not None else value
