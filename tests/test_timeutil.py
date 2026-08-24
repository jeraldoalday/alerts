"""UTC normalisation, and the offset it used to throw away.

Everything this project stores is UTC, so most of `as_utc_naive`'s work is a
no-op it has to be careful about. The cases that matter are the ones where
the value did not start out in UTC -- which a config file can produce, and a
database column never can.
"""
from datetime import datetime, timedelta, timezone

from alerts.config import MaintenanceWindowConfig
from alerts.maintenance import active_windows, is_in_maintenance
from alerts.timeutil import as_utc_naive, utcnow

BERLIN = timezone(timedelta(hours=2))
CHICAGO = timezone(timedelta(hours=-5))


def test_a_naive_value_is_returned_untouched():
    value = datetime(2026, 9, 1, 1, 0)

    assert as_utc_naive(value) == value
    assert as_utc_naive(value).tzinfo is None


def test_a_utc_aware_value_keeps_its_clock_reading():
    value = datetime(2026, 9, 1, 1, 0, tzinfo=timezone.utc)

    assert as_utc_naive(value) == datetime(2026, 9, 1, 1, 0)


def test_utcnow_round_trips_through_it():
    now = utcnow()

    assert as_utc_naive(now) == now.replace(tzinfo=None)


def test_an_eastern_offset_is_converted_back_to_utc():
    """01:00 in Berlin is 23:00 the day before, not 01:00."""
    value = datetime(2026, 9, 1, 1, 0, tzinfo=BERLIN)

    assert as_utc_naive(value) == datetime(2026, 8, 31, 23, 0)


def test_a_western_offset_is_converted_forward():
    value = datetime(2026, 9, 1, 22, 0, tzinfo=CHICAGO)

    assert as_utc_naive(value) == datetime(2026, 9, 2, 3, 0)


def test_the_result_is_always_naive():
    for value in (
        datetime(2026, 9, 1, 1, 0),
        datetime(2026, 9, 1, 1, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 1, 1, 0, tzinfo=BERLIN),
    ):
        assert as_utc_naive(value).tzinfo is None


def test_converting_twice_changes_nothing():
    once = as_utc_naive(datetime(2026, 9, 1, 1, 0, tzinfo=BERLIN))

    assert as_utc_naive(once) == once


def test_two_spellings_of_the_same_moment_normalise_to_one_value():
    """The property the whole module exists for."""
    in_utc = datetime(2026, 8, 31, 23, 0, tzinfo=timezone.utc)
    in_berlin = datetime(2026, 9, 1, 1, 0, tzinfo=BERLIN)

    assert as_utc_naive(in_utc) == as_utc_naive(in_berlin)


# --- what the offset was costing -------------------------------------------


def _window(**overrides) -> MaintenanceWindowConfig:
    fields = dict(
        name="gateway-migration",
        starts_at="2026-09-01T01:00:00+02:00",
        ends_at="2026-09-01T05:00:00+02:00",
    )
    fields.update(overrides)
    return MaintenanceWindowConfig(**fields)


def _at(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def test_a_window_declared_in_another_offset_covers_the_hours_it_names():
    """01:00-05:00 Berlin is 23:00-03:00 UTC, and used to be neither.

    Discarding the offset filed the window as 01:00-05:00 UTC: it suppressed
    nothing for the first two hours of the change it was written for, then
    went on suppressing alerts for two hours after it had ended. Nothing
    about that is visible in the config, in the schedule the report prints,
    or in alerting that simply didn't happen.
    """
    window = _window()

    assert is_in_maintenance([window], "api", now=_at("2026-08-31T23:30:00+00:00"))
    assert is_in_maintenance([window], "api", now=_at("2026-09-01T02:59:00+00:00"))
    assert not is_in_maintenance([window], "api", now=_at("2026-09-01T04:30:00+00:00"))


def test_the_window_does_not_start_early_either():
    window = _window()

    assert not is_in_maintenance([window], "api", now=_at("2026-08-31T22:30:00+00:00"))


def test_the_half_open_boundary_survives_the_conversion():
    """The start is covered, the end is not -- in UTC terms after converting."""
    window = _window()

    assert is_in_maintenance([window], "api", now=_at("2026-08-31T23:00:00+00:00"))
    assert not is_in_maintenance([window], "api", now=_at("2026-09-01T03:00:00+00:00"))


def test_a_window_declared_in_utc_is_unaffected():
    window = _window(
        starts_at="2026-09-01T01:00:00Z", ends_at="2026-09-01T05:00:00Z"
    )

    assert is_in_maintenance([window], "api", now=_at("2026-09-01T03:00:00+00:00"))
    assert not is_in_maintenance([window], "api", now=_at("2026-08-31T23:30:00+00:00"))


def test_a_window_declared_without_an_offset_is_read_as_utc():
    """The project's own convention: a bare timestamp is UTC."""
    window = _window(
        starts_at="2026-09-01T01:00:00", ends_at="2026-09-01T05:00:00"
    )

    assert is_in_maintenance([window], "api", now=_at("2026-09-01T03:00:00+00:00"))


def test_the_occurrence_reports_the_converted_span():
    occurrence = active_windows(
        [_window()], "api", now=_at("2026-09-01T00:00:00+00:00")
    )[0]

    assert occurrence.starts_at == datetime(2026, 8, 31, 23, 0)
    assert occurrence.ends_at == datetime(2026, 9, 1, 3, 0)
    assert occurrence.duration == timedelta(hours=4)
