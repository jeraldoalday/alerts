"""The `alerts maintenance` report: rows, table rendering, and the subcommand."""
from datetime import datetime

import pytest

from alerts import cli
from alerts.config import MaintenanceWindowConfig
from alerts.reporting import (
    MaintenanceRow,
    collect_maintenance_rows,
    format_maintenance_summary,
    format_maintenance_table,
)

# 2026-09-01 is a Tuesday; 2026-09-06 is the following Sunday.
NOW = datetime(2026, 9, 1, 12, 0)


def _recurring(**overrides) -> MaintenanceWindowConfig:
    defaults = dict(name="nightly", start="02:00", duration_minutes=60)
    defaults.update(overrides)
    return MaintenanceWindowConfig(**defaults)


def _row(**overrides) -> MaintenanceRow:
    defaults = dict(
        name="nightly",
        schedule="daily at 02:00 UTC for 60m",
        monitors=[],
        active_until=None,
        next_start=datetime(2026, 9, 2, 2, 0),
    )
    defaults.update(overrides)
    return MaintenanceRow(**defaults)


def _write_config(tmp_path, body: str) -> str:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(body)
    return str(config_file)


CONFIG = """
monitors:
  - name: web
    type: http
    target: https://example.com
  - name: api
    type: http
    target: https://api.example.com

maintenance:
  - name: freeze
    start: "00:00"
    duration_minutes: 1440
  - name: api-change
    monitors: [api]
    starts_at: 2026-09-01T01:00:00Z
    ends_at: 2026-09-01T05:00:00Z
"""


def test_a_window_running_now_reports_when_it_ends():
    rows = collect_maintenance_rows([_recurring(start="00:00", duration_minutes=1440)], now=NOW)

    assert rows[0].is_active is True
    assert rows[0].active_until == datetime(2026, 9, 2, 0, 0)


def test_a_window_not_running_reports_its_next_start():
    rows = collect_maintenance_rows([_recurring()], now=NOW)

    assert rows[0].is_active is False
    assert rows[0].next_start == datetime(2026, 9, 2, 2, 0)


def test_a_weekday_window_reports_the_right_next_start():
    rows = collect_maintenance_rows([_recurring(days=["sun"])], now=NOW)

    assert rows[0].next_start == datetime(2026, 9, 6, 2, 0)


def test_a_past_fixed_window_has_no_next_start():
    window = MaintenanceWindowConfig(
        name="done", starts_at="2026-08-01T01:00:00Z", ends_at="2026-08-01T02:00:00Z"
    )

    rows = collect_maintenance_rows([window], now=NOW)

    assert rows[0].next_start is None
    assert rows[0].is_active is False


def test_rows_carry_the_schedule_description():
    rows = collect_maintenance_rows([_recurring(days=["mon", "wed"])], now=NOW)

    assert rows[0].schedule == "mon,wed at 02:00 UTC for 60m"


def test_filtering_by_monitor_keeps_estate_wide_windows():
    windows = [_recurring(name="everything"), _recurring(name="api-only", monitors=["api"])]

    rows = collect_maintenance_rows(windows, monitor="web", now=NOW)

    assert [row.name for row in rows] == ["everything"]


def test_filtering_by_monitor_keeps_windows_naming_it():
    windows = [_recurring(name="api-only", monitors=["api"])]

    rows = collect_maintenance_rows(windows, monitor="api", now=NOW)

    assert [row.name for row in rows] == ["api-only"]


def test_a_scoped_window_is_reported_active_without_a_monitor_filter():
    """The schedule doesn't vary per monitor, so any covered one answers it."""
    window = _recurring(name="scoped", monitors=["api"], start="00:00", duration_minutes=1440)

    rows = collect_maintenance_rows([window], now=NOW)

    assert rows[0].is_active is True


def test_table_marks_an_active_window():
    table = format_maintenance_table(
        [_row(active_until=datetime(2026, 9, 1, 14, 0))], now=NOW
    )

    assert "ACTIVE, 2h 0m left" in table


def test_table_shows_the_wait_for_an_upcoming_window():
    table = format_maintenance_table([_row()], now=NOW)

    assert "in 14h 0m" in table


def test_table_marks_a_window_with_nothing_scheduled():
    table = format_maintenance_table([_row(next_start=None)], now=NOW)

    assert "past" in table


def test_table_renders_all_for_an_unscoped_window():
    table = format_maintenance_table([_row(monitors=[])], now=NOW)

    assert " all " in table


def test_table_lists_the_scoped_monitors():
    table = format_maintenance_table([_row(monitors=["web", "api"])], now=NOW)

    assert "web,api" in table


def test_table_says_so_when_nothing_is_configured():
    assert format_maintenance_table([], now=NOW) == "No maintenance windows configured."


def test_summary_counts_windows_and_active_ones():
    rows = [_row(), _row(active_until=datetime(2026, 9, 1, 14, 0))]

    assert format_maintenance_summary(rows) == "2 window(s), 1 active"


def test_parse_args_maintenance_defaults():
    args = cli._parse_args(["maintenance"])

    assert args.command == "maintenance"
    assert args.config == "config.yaml"
    assert args.monitor is None
    assert args.exit_code is False


def test_parse_args_maintenance_accepts_a_monitor_filter():
    args = cli._parse_args(["maintenance", "--monitor", "api", "--exit-code"])

    assert args.monitor == "api"
    assert args.exit_code is True


def test_main_maintenance_prints_every_window(tmp_path, capsys):
    exit_code = cli.main(["maintenance", "--config", _write_config(tmp_path, CONFIG)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "freeze" in output
    assert "api-change" in output
    assert "2 window(s)" in output


def test_main_maintenance_filters_by_monitor(tmp_path, capsys):
    exit_code = cli.main(
        ["maintenance", "--config", _write_config(tmp_path, CONFIG), "--monitor", "web"]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "freeze" in output
    assert "api-change" not in output


def test_main_maintenance_reports_no_windows(tmp_path, capsys):
    config = _write_config(tmp_path, "monitors: []\n")

    exit_code = cli.main(["maintenance", "--config", config])

    assert exit_code == 0
    assert "No maintenance windows configured." in capsys.readouterr().out


def test_main_maintenance_exit_code_fails_when_nothing_is_active(tmp_path, capsys):
    config = _write_config(
        tmp_path,
        "maintenance:\n"
        "  - name: past\n"
        "    starts_at: 2025-01-01T00:00:00Z\n"
        "    ends_at: 2025-01-01T01:00:00Z\n",
    )

    exit_code = cli.main(["maintenance", "--config", config, "--exit-code"])

    capsys.readouterr()
    assert exit_code == 1


def test_main_maintenance_exit_code_passes_inside_a_window(tmp_path, capsys):
    config = _write_config(
        tmp_path,
        "maintenance:\n"
        "  - name: always\n"
        '    start: "00:00"\n'
        "    duration_minutes: 1440\n",
    )

    exit_code = cli.main(["maintenance", "--config", config, "--exit-code"])

    capsys.readouterr()
    assert exit_code == 0


def test_main_maintenance_does_not_need_a_database(tmp_path, capsys):
    """A window is a declaration, so the report reads config only."""
    config = _write_config(
        tmp_path,
        f"database:\n  url: sqlite:///{tmp_path / 'never-created.db'}\n"
        "maintenance:\n"
        "  - name: nightly\n"
        '    start: "02:00"\n'
        "    duration_minutes: 30\n",
    )

    cli.main(["maintenance", "--config", config])

    capsys.readouterr()
    assert not (tmp_path / "never-created.db").exists()


def test_main_maintenance_surfaces_a_bad_config(tmp_path):
    config = _write_config(
        tmp_path, "maintenance:\n  - name: broken\n    duration_minutes: 30\n"
    )

    with pytest.raises(Exception, match="needs both start and duration_minutes"):
        cli.main(["maintenance", "--config", config])
