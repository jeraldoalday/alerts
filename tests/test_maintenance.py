"""Maintenance window configuration and schedule evaluation.

Times are UTC throughout, and `now` is always passed explicitly so these
never depend on when they run.
"""
from datetime import datetime, timezone

import pytest

from alerts.config import ConfigError, MaintenanceWindowConfig, load_config
from alerts.maintenance import (
    active_windows,
    is_in_maintenance,
    occurrences_around,
)


def _recurring(**overrides) -> MaintenanceWindowConfig:
    defaults = dict(name="nightly", start="02:00", duration_minutes=60)
    defaults.update(overrides)
    return MaintenanceWindowConfig(**defaults)


def _fixed(**overrides) -> MaintenanceWindowConfig:
    defaults = dict(
        name="migration",
        starts_at="2026-09-01T01:00:00Z",
        ends_at="2026-09-01T05:00:00Z",
    )
    defaults.update(overrides)
    return MaintenanceWindowConfig(**defaults)


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    """A moment in September 2026. The 1st is a Tuesday."""
    return datetime(2026, 9, day, hour, minute)


# --- configuration ---------------------------------------------------------


def test_a_recurring_window_needs_a_duration():
    with pytest.raises(ValueError, match="needs both start and duration_minutes"):
        MaintenanceWindowConfig(name="nightly", start="02:00")


def test_a_recurring_window_needs_a_start():
    with pytest.raises(ValueError, match="needs both start and duration_minutes"):
        MaintenanceWindowConfig(name="nightly", duration_minutes=30)


def test_a_fixed_window_needs_both_ends():
    with pytest.raises(ValueError, match="needs both starts_at and ends_at"):
        MaintenanceWindowConfig(name="once", starts_at="2026-09-01T01:00:00Z")


def test_a_window_cannot_mix_the_two_forms():
    with pytest.raises(ValueError, match="mixes the recurring form"):
        MaintenanceWindowConfig(
            name="confused",
            start="02:00",
            duration_minutes=30,
            starts_at="2026-09-01T01:00:00Z",
            ends_at="2026-09-01T05:00:00Z",
        )


def test_a_window_with_no_schedule_at_all_is_rejected():
    with pytest.raises(ValueError, match="declares no schedule"):
        MaintenanceWindowConfig(name="empty")


def test_a_fixed_window_must_end_after_it_starts():
    with pytest.raises(ValueError, match="ends at or before it starts"):
        MaintenanceWindowConfig(
            name="backwards",
            starts_at="2026-09-01T05:00:00Z",
            ends_at="2026-09-01T01:00:00Z",
        )


def test_a_fixed_window_rejects_a_days_filter():
    with pytest.raises(ValueError, match="already names the exact dates"):
        MaintenanceWindowConfig(
            name="once",
            starts_at="2026-09-01T01:00:00Z",
            ends_at="2026-09-01T05:00:00Z",
            days=["mon"],
        )


def test_day_names_are_normalised():
    window = _recurring(days=["Monday", " TUE", "wed"])

    assert window.days == ["mon", "tue", "wed"]


def test_unknown_day_names_are_rejected():
    with pytest.raises(ValueError, match="unknown day"):
        _recurring(days=["funday"])


def test_a_window_without_monitors_applies_to_everything():
    window = _recurring()

    assert window.applies_to("anything") is True


def test_a_scoped_window_applies_only_to_its_monitors():
    window = _recurring(monitors=["api"])

    assert window.applies_to("api") is True
    assert window.applies_to("web") is False


def test_load_config_rejects_a_window_naming_an_unknown_monitor(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "monitors:\n"
        "  - name: web\n"
        "    type: http\n"
        "    target: https://example.com\n"
        "maintenance:\n"
        "  - name: typo\n"
        "    monitors: [wbe]\n"
        "    start: '02:00'\n"
        "    duration_minutes: 30\n"
    )

    with pytest.raises(ConfigError, match="unknown monitor"):
        load_config(config_file)


def test_load_config_accepts_a_window_naming_a_real_monitor(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "monitors:\n"
        "  - name: web\n"
        "    type: http\n"
        "    target: https://example.com\n"
        "maintenance:\n"
        "  - name: nightly\n"
        "    monitors: [web]\n"
        "    start: '02:00'\n"
        "    duration_minutes: 30\n"
    )

    config = load_config(config_file)

    assert config.maintenance[0].monitors == ["web"]


def test_maintenance_defaults_to_empty(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("monitors: []\n")

    assert load_config(config_file).maintenance == []


# --- recurring evaluation --------------------------------------------------


def test_a_moment_inside_a_recurring_window_is_maintenance():
    assert is_in_maintenance([_recurring()], "web", now=_at(1, 2, 30)) is True


def test_the_start_instant_is_inside_the_window():
    assert is_in_maintenance([_recurring()], "web", now=_at(1, 2, 0)) is True


def test_the_end_instant_is_outside_the_window():
    """Half-open, so back-to-back windows don't both claim the boundary."""
    assert is_in_maintenance([_recurring()], "web", now=_at(1, 3, 0)) is False


def test_a_moment_before_the_window_is_not_maintenance():
    assert is_in_maintenance([_recurring()], "web", now=_at(1, 1, 59)) is False


def test_a_window_running_past_midnight_covers_the_early_hours():
    window = _recurring(start="23:00", duration_minutes=120)

    assert is_in_maintenance([window], "web", now=_at(1, 23, 30)) is True
    assert is_in_maintenance([window], "web", now=_at(2, 0, 30)) is True
    assert is_in_maintenance([window], "web", now=_at(2, 1, 30)) is False


def test_a_days_filter_matches_the_day_the_window_starts_on():
    """A Sunday-night window is still a Sunday window at 00:30 on Monday."""
    window = _recurring(start="23:00", duration_minutes=120, days=["sun"])

    # 2026-09-06 is a Sunday.
    assert is_in_maintenance([window], "web", now=_at(6, 23, 30)) is True
    assert is_in_maintenance([window], "web", now=_at(7, 0, 30)) is True
    assert is_in_maintenance([window], "web", now=_at(7, 23, 30)) is False


def test_a_days_filter_excludes_other_days():
    window = _recurring(days=["sun"])

    assert is_in_maintenance([window], "web", now=_at(1, 2, 30)) is False


def test_a_window_scoped_to_another_monitor_does_not_suppress():
    window = _recurring(monitors=["api"])

    assert is_in_maintenance([window], "web", now=_at(1, 2, 30)) is False
    assert is_in_maintenance([window], "api", now=_at(1, 2, 30)) is True


def test_an_aware_now_is_handled_like_a_naive_one():
    aware = datetime(2026, 9, 1, 2, 30, tzinfo=timezone.utc)

    assert is_in_maintenance([_recurring()], "web", now=aware) is True


def test_occurrences_are_resolved_for_both_candidate_days():
    occurrences = occurrences_around(_recurring(), _at(2, 12))

    assert [o.starts_at for o in occurrences] == [_at(2, 2), _at(1, 2)]


def test_an_occurrence_reports_its_duration():
    occurrence = occurrences_around(_recurring(duration_minutes=90), _at(1, 12))[0]

    assert occurrence.duration.total_seconds() == 90 * 60


# --- fixed evaluation ------------------------------------------------------


def test_a_moment_inside_a_fixed_window_is_maintenance():
    assert is_in_maintenance([_fixed()], "web", now=_at(1, 3)) is True


def test_a_moment_outside_a_fixed_window_is_not():
    assert is_in_maintenance([_fixed()], "web", now=_at(1, 6)) is False


def test_a_fixed_window_does_not_repeat_the_next_day():
    assert is_in_maintenance([_fixed()], "web", now=_at(2, 3)) is False


def test_a_fixed_windows_end_instant_is_outside_it():
    assert is_in_maintenance([_fixed()], "web", now=_at(1, 5)) is False


# --- multiple windows ------------------------------------------------------


def test_no_windows_configured_is_never_maintenance():
    assert is_in_maintenance([], "web", now=_at(1, 2, 30)) is False


def test_any_matching_window_suppresses():
    windows = [_recurring(name="a", start="02:00", duration_minutes=30), _fixed()]

    assert is_in_maintenance(windows, "web", now=_at(1, 4)) is True


def test_active_windows_names_every_overlapping_window():
    windows = [
        _recurring(name="estate-freeze", start="00:00", duration_minutes=24 * 60),
        _recurring(name="db-patch", start="02:00", duration_minutes=60),
    ]

    active = active_windows(windows, "web", now=_at(1, 2, 30))

    assert sorted(o.name for o in active) == ["db-patch", "estate-freeze"]


def test_active_windows_is_empty_outside_every_window():
    windows = [_recurring(), _fixed()]

    assert active_windows(windows, "web", now=_at(1, 12)) == []
