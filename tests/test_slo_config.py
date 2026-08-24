"""SLO targets as declared in the config.

Two of these are about refusing a setting that would otherwise look
configured and do nothing -- a window with no objective, and a target the
window is arithmetically unable to hold. Both fail at load time on purpose:
the alternative is discovering the problem from a report that has been
quietly wrong since the day it was written.
"""
import pytest
from pydantic import ValidationError

from alerts.config import ConfigError, MonitorConfig, load_config
from alerts.slo import DEFAULT_WINDOW_DAYS


def _monitor(**overrides) -> MonitorConfig:
    fields = {"name": "api", "type": "http", "target": "https://example.com"}
    fields.update(overrides)
    return MonitorConfig(**fields)


def _write(tmp_path, body: str) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(body)
    return str(path)


def test_a_monitor_has_no_objective_by_default():
    monitor = _monitor()

    assert monitor.slo_target is None
    assert monitor.slo_window_days == DEFAULT_WINDOW_DAYS


def test_a_target_is_kept_as_declared():
    monitor = _monitor(slo_target=99.9, slo_window_days=7, interval_seconds=60)

    assert monitor.slo_target == 99.9
    assert monitor.slo_window_days == 7


def test_a_target_of_a_hundred_percent_is_allowed():
    """An unattainable promise is still a promise someone may want to make."""
    assert _monitor(slo_target=100).slo_target == 100


def test_a_target_of_zero_is_refused():
    with pytest.raises(ValidationError, match="greater than 0"):
        _monitor(slo_target=0)


def test_a_target_above_a_hundred_is_refused():
    with pytest.raises(ValidationError, match="less than or equal to 100"):
        _monitor(slo_target=100.5)


def test_a_window_shorter_than_a_day_is_refused():
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        _monitor(slo_target=99.0, slo_window_days=0)


def test_a_window_longer_than_a_year_is_refused():
    with pytest.raises(ValidationError, match="less than or equal to 365"):
        _monitor(slo_target=99.0, slo_window_days=400)


def test_a_window_without_a_target_is_refused():
    """Nothing reads the window except the budget, so it would do nothing."""
    with pytest.raises(ValidationError, match="no slo_target"):
        _monitor(slo_window_days=7)


def test_the_default_window_alone_is_not_treated_as_declaring_one():
    """Only an explicitly set window is refused, not the field's default."""
    monitor = _monitor()

    assert monitor.slo_window_days == DEFAULT_WINDOW_DAYS


def test_a_target_the_window_cannot_afford_one_failure_of_is_refused():
    """288 checks a day at 0.01% affords 0.03 failures -- one blip breaches."""
    with pytest.raises(ValidationError, match="affords 0.03 failed checks"):
        _monitor(
            slo_target=99.99, slo_window_days=1, interval_seconds=300
        )


def test_the_same_target_is_accepted_once_the_window_is_long_enough():
    monitor = _monitor(slo_target=99.99, slo_window_days=60, interval_seconds=300)

    assert monitor.slo_target == 99.99


def test_the_same_window_is_accepted_once_the_interval_is_short_enough():
    monitor = _monitor(slo_target=99.99, slo_window_days=1, interval_seconds=5)

    assert monitor.slo_target == 99.99


def test_the_affordability_error_names_the_three_knobs_that_fix_it():
    with pytest.raises(ValidationError) as caught:
        _monitor(slo_target=99.99, slo_window_days=1, interval_seconds=300)

    message = str(caught.value)
    assert "slo_window_days" in message
    assert "slo_target" in message
    assert "interval_seconds" in message


def test_a_hundred_percent_target_is_exempt_from_the_affordability_check():
    """Zero budget at 100% is the point of the setting, not an accident.

    Every other target that affords nothing got there by arithmetic nobody
    intended. Someone typing 100 is deliberately saying every failure
    counts, so the check that catches accidents doesn't apply.
    """
    monitor = _monitor(slo_target=100, slo_window_days=1, interval_seconds=3600)

    assert monitor.slo_target == 100


def test_a_target_loads_from_yaml(tmp_path):
    path = _write(
        tmp_path,
        """
monitors:
  - name: api
    type: http
    target: https://example.com
    interval_seconds: 60
    slo_target: 99.9
    slo_window_days: 30
""",
    )

    config = load_config(path)

    assert config.monitors[0].slo_target == 99.9
    assert config.monitors[0].slo_window_days == 30


def test_an_unaffordable_target_fails_the_whole_load(tmp_path):
    """A config error, not a monitor quietly dropped from the estate."""
    path = _write(
        tmp_path,
        """
monitors:
  - name: api
    type: http
    target: https://example.com
    interval_seconds: 3600
    slo_target: 99.9
    slo_window_days: 1
""",
    )

    with pytest.raises(ConfigError, match="failed checks"):
        load_config(path)


def test_the_scheduler_default_interval_is_what_affordability_is_judged_against(
    tmp_path,
):
    """The backfilled interval is the one the monitor will actually run at.

    A monitor omitting `interval_seconds` inherits
    `scheduler.default_interval_seconds`, and that backfill happens before
    this validator sees the monitor -- so a target judged affordable here is
    affordable at the interval the scheduler will really use, not at the
    field default it never had.
    """
    path = _write(
        tmp_path,
        """
scheduler:
  default_interval_seconds: 3600
monitors:
  - name: api
    type: http
    target: https://example.com
    slo_target: 99.9
    slo_window_days: 1
""",
    )

    with pytest.raises(ConfigError, match="3600s interval"):
        load_config(path)


def test_a_monitor_without_a_target_is_unaffected_by_any_of_this(tmp_path):
    path = _write(
        tmp_path,
        """
monitors:
  - name: api
    type: http
    target: https://example.com
    interval_seconds: 3600
""",
    )

    config = load_config(path)

    assert config.monitors[0].slo_target is None


def test_targets_are_independent_per_monitor():
    monitors = [
        _monitor(name="a", slo_target=99.9),
        _monitor(name="b"),
        _monitor(name="c", slo_target=95.0, slo_window_days=7),
    ]

    assert [m.slo_target for m in monitors] == [99.9, None, 95.0]
    assert [m.slo_window_days for m in monitors] == [30, 30, 7]
