"""The `alerts slo` report: the tally query, the rows, the table, the command.

The report is only worth anything if it measures each monitor over its own
window and out of the same history the scheduler is writing, so the tally
query gets pinned against real rows on both sides of a cutoff rather than
mocked.
"""
from datetime import timedelta

from alerts import cli
from alerts.db import Database
from alerts.models import CheckResult, Monitor, MonitorState, MonitorType
from alerts.reporting import (
    SloRow,
    collect_slo_rows,
    format_slo_summary,
    format_slo_table,
)
from alerts.slo import BudgetStatus, compute_error_budget
from alerts.timeutil import utcnow
from alerts.web import queries

NOW = utcnow().replace(tzinfo=None)


def _monitor(session, name="api", **overrides) -> Monitor:
    fields = dict(
        name=name,
        type=MonitorType.HTTP,
        target="https://example.com",
        state=MonitorState.UP,
        slo_target=99.0,
        slo_window_days=30,
        tags=[],
        enabled=True,
    )
    fields.update(overrides)
    monitor = Monitor(**fields)
    session.add(monitor)
    session.flush()
    return monitor


def _checks(session, monitor_id, *, total, failed, minutes_apart=1, age_offset=0):
    for index in range(total):
        session.add(
            CheckResult(
                monitor_id=monitor_id,
                success=index >= failed,
                latency_ms=20.0,
                checked_at=NOW - timedelta(minutes=age_offset + index * minutes_apart),
            )
        )


def _row(**overrides) -> SloRow:
    budget = overrides.pop(
        "budget",
        compute_error_budget(target_percentage=99.0, total_checks=1000, failed_checks=1),
    )
    fields = dict(name="api", budget=budget)
    fields.update(overrides)
    return SloRow(**fields)


# --- the tally query -------------------------------------------------------


def test_a_tally_counts_totals_and_failures(db):
    with db.session() as session:
        monitor = _monitor(session)
        _checks(session, monitor.id, total=10, failed=3)

    with db.session() as session:
        tally = queries.check_tally_since(session, 1, NOW - timedelta(days=1))

    assert tally.total == 10
    assert tally.failed == 3
    assert tally.successful == 7


def test_a_tally_ignores_checks_from_before_the_cutoff(db):
    """The window is the whole point: older history must not leak in."""
    with db.session() as session:
        monitor = _monitor(session)
        _checks(session, monitor.id, total=5, failed=0)
        _checks(session, monitor.id, total=5, failed=5, age_offset=60 * 24 * 40)

    with db.session() as session:
        tally = queries.check_tally_since(session, 1, NOW - timedelta(days=30))

    assert tally.total == 5
    assert tally.failed == 0


def test_a_tally_without_a_cutoff_counts_everything_retained(db):
    with db.session() as session:
        monitor = _monitor(session)
        _checks(session, monitor.id, total=3, failed=1)
        _checks(session, monitor.id, total=2, failed=2, age_offset=60 * 24 * 400)

    with db.session() as session:
        tally = queries.check_tally_since(session, 1)

    assert tally.total == 5
    assert tally.failed == 3


def test_a_tally_ignores_other_monitors_checks(db):
    with db.session() as session:
        first = _monitor(session, "api")
        second = _monitor(session, "web")
        _checks(session, first.id, total=4, failed=0)
        _checks(session, second.id, total=6, failed=6)

    with db.session() as session:
        assert queries.check_tally_since(session, first.id).total == 4
        assert queries.check_tally_since(session, second.id).failed == 6


def test_a_monitor_with_no_checks_tallies_zero(db):
    with db.session() as session:
        _monitor(session)

    with db.session() as session:
        tally = queries.check_tally_since(session, 1, NOW - timedelta(days=30))

    assert (tally.total, tally.failed) == (0, 0)


# --- the budget builder ----------------------------------------------------


def test_a_monitor_without_a_target_has_no_budget(db):
    with db.session() as session:
        monitor = _monitor(session, slo_target=None)
        assert queries.error_budget(session, monitor) is None


def test_a_budget_is_measured_over_the_monitor_s_own_window(db):
    """Two monitors, the same history, different windows, different figures."""
    with db.session() as session:
        wide = _monitor(session, "wide", slo_window_days=30)
        narrow = _monitor(session, "narrow", slo_window_days=1)
        for monitor in (wide, narrow):
            _checks(session, monitor.id, total=60, failed=0)
            _checks(session, monitor.id, total=40, failed=40, age_offset=60 * 24 * 10)

    with db.session() as session:
        wide = queries.get_monitor_by_name(session, "wide")
        narrow = queries.get_monitor_by_name(session, "narrow")

        assert queries.error_budget(session, wide, now=NOW).total_checks == 100
        assert queries.error_budget(session, narrow, now=NOW).total_checks == 60


def test_a_budget_carries_the_monitor_s_target_and_window(db):
    with db.session() as session:
        monitor = _monitor(session, slo_target=99.95, slo_window_days=7)
        _checks(session, monitor.id, total=10, failed=0)

    with db.session() as session:
        budget = queries.error_budget(session, queries.get_monitor_by_name(session, "api"))

    assert budget.target_percentage == 99.95
    assert budget.window_days == 7


# --- the rows --------------------------------------------------------------


def test_only_monitors_with_a_target_get_a_row(db):
    with db.session() as session:
        _monitor(session, "watched", slo_target=99.0)
        _monitor(session, "unwatched", slo_target=None)

    with db.session() as session:
        rows = collect_slo_rows(session)

    assert [row.name for row in rows] == ["watched"]


def test_rows_can_be_narrowed_to_one_tag(db):
    with db.session() as session:
        _monitor(session, "web", tags=["prod"])
        _monitor(session, "batch", tags=["internal"])

    with db.session() as session:
        rows = collect_slo_rows(session, tag="prod")

    assert [row.name for row in rows] == ["web"]


def test_a_tag_filter_is_folded_to_the_stored_spelling(db):
    with db.session() as session:
        _monitor(session, "web", tags=["prod"])

    with db.session() as session:
        assert len(collect_slo_rows(session, tag="PROD")) == 1


def test_rows_can_be_narrowed_to_one_monitor(db):
    with db.session() as session:
        _monitor(session, "web")
        _monitor(session, "api")

    with db.session() as session:
        rows = collect_slo_rows(session, monitor="api")

    assert [row.name for row in rows] == ["api"]


def test_the_two_filters_narrow_rather_than_override(db):
    with db.session() as session:
        _monitor(session, "api", tags=["internal"])

    with db.session() as session:
        assert collect_slo_rows(session, monitor="api", tag="prod") == []


def test_a_row_carries_the_monitor_s_tags_and_paused_flag(db):
    with db.session() as session:
        _monitor(session, "api", tags=["prod", "api"], enabled=False)

    with db.session() as session:
        row = collect_slo_rows(session)[0]

    assert row.tags == ("prod", "api")
    assert row.paused is True


# --- the exit code ---------------------------------------------------------


def test_a_breaching_row_is_reported_as_breaching():
    budget = compute_error_budget(
        target_percentage=99.0, total_checks=1000, failed_checks=50
    )

    assert _row(budget=budget).is_breaching is True


def test_a_paused_row_never_breaches_however_bad_it_looks():
    """Its window slides over a history that stopped growing."""
    budget = compute_error_budget(
        target_percentage=99.0, total_checks=1000, failed_checks=500
    )

    assert _row(budget=budget, paused=True).is_breaching is False


def test_a_row_with_no_data_does_not_breach():
    budget = compute_error_budget(target_percentage=99.0, total_checks=0, failed_checks=0)

    row = _row(budget=budget)
    assert row.status is BudgetStatus.NO_DATA
    assert row.is_breaching is False


# --- the table -------------------------------------------------------------


def test_an_empty_report_says_nothing_has_a_target():
    assert format_slo_table([]) == "No monitors have an SLO target."


def test_the_table_shows_target_and_actual_at_two_decimal_places():
    """A report rounding to one place can't show a 99.95% target missed."""
    budget = compute_error_budget(
        target_percentage=99.95, total_checks=10_000, failed_checks=4
    )

    table = format_slo_table([_row(budget=budget)])

    assert "99.95%" in table
    assert "99.96%" in table


def test_the_table_reports_the_window_in_days():
    budget = compute_error_budget(
        target_percentage=99.0, total_checks=100, failed_checks=0, window_days=7
    )

    assert "7d" in format_slo_table([_row(budget=budget)])


def test_a_spotless_budget_reads_as_fully_remaining():
    budget = compute_error_budget(target_percentage=99.0, total_checks=1000, failed_checks=0)

    assert "100% left" in format_slo_table([_row(budget=budget)])


def test_an_overrun_budget_reads_as_spent_rather_than_zero_percent():
    budget = compute_error_budget(target_percentage=99.0, total_checks=1000, failed_checks=50)

    table = format_slo_table([_row(budget=budget)])

    assert "spent" in table
    assert "BREACHED" in table


def test_the_two_statuses_worth_acting_on_are_in_capitals():
    at_risk = compute_error_budget(
        target_percentage=99.0, total_checks=1000, failed_checks=8
    )
    breached = compute_error_budget(
        target_percentage=99.0, total_checks=1000, failed_checks=80
    )

    assert "AT RISK" in format_slo_table([_row(budget=at_risk)])
    assert "BREACHED" in format_slo_table([_row(budget=breached)])


def test_a_window_with_no_checks_shows_dashes_rather_than_zero_percent():
    budget = compute_error_budget(target_percentage=99.0, total_checks=0, failed_checks=0)

    table = format_slo_table([_row(budget=budget)])

    assert "no data" in table
    assert "  -  " in table or table.rstrip().endswith("-")


def test_the_note_translates_the_budget_into_minutes():
    budget = compute_error_budget(
        target_percentage=99.9, total_checks=1000, failed_checks=0, window_days=30
    )

    assert "43m of 43m left" in format_slo_table([_row(budget=budget)])


def test_a_paused_row_says_so_in_the_note():
    assert "paused, not in config" in format_slo_table([_row(paused=True)])


def test_the_table_has_no_trailing_whitespace():
    lines = format_slo_table([_row()]).split("\n")

    assert all(line == line.rstrip() for line in lines)


# --- the summary -----------------------------------------------------------


def test_an_empty_summary_counts_nothing():
    assert format_slo_summary([]) == "0 monitor(s) with an objective"


def test_the_summary_tallies_each_status():
    ok = compute_error_budget(target_percentage=99.0, total_checks=1000, failed_checks=0)
    breached = compute_error_budget(
        target_percentage=99.0, total_checks=1000, failed_checks=80
    )

    summary = format_slo_summary(
        [_row(name="a", budget=ok), _row(name="b", budget=breached)]
    )

    assert summary.startswith("2 monitor(s) with an objective:")
    assert "1 ok" in summary
    assert "1 breached" in summary


def test_the_summary_counts_paused_monitors_on_their_own():
    summary = format_slo_summary([_row(), _row(name="b", paused=True)])

    assert "1 paused" in summary
    assert "2 monitor(s)" in summary


def test_the_summary_omits_statuses_nothing_is_in():
    summary = format_slo_summary([_row()])

    assert "breached" not in summary
    assert "no data" not in summary


# --- the command -----------------------------------------------------------


def _config(tmp_path) -> tuple:
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "test.db"
    config_file.write_text(f"database:\n  url: sqlite:///{db_path}\n")
    database = Database(f"sqlite:///{db_path}")
    database.create_all()
    return str(config_file), database


def test_parse_args_slo_defaults():
    args = cli._parse_args(["slo"])

    assert args.command == "slo"
    assert args.config == "config.yaml"
    assert args.monitor is None
    assert args.tag is None
    assert args.exit_code is False


def test_parse_args_slo_accepts_its_filters():
    args = cli._parse_args(["slo", "--monitor", "api", "--tag", "prod", "--exit-code"])

    assert (args.monitor, args.tag, args.exit_code) == ("api", "prod", True)


def test_the_command_prints_the_table_and_the_summary(tmp_path, capsys):
    config_path, database = _config(tmp_path)
    with database.session() as session:
        monitor = _monitor(session, "api", slo_target=99.0)
        _checks(session, monitor.id, total=100, failed=0)

    exit_code = cli.main(["slo", "--config", config_path])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "api" in output
    assert "1 monitor(s) with an objective" in output


def test_the_command_exits_zero_on_a_healthy_budget_under_exit_code(tmp_path, capsys):
    config_path, database = _config(tmp_path)
    with database.session() as session:
        monitor = _monitor(session, "api", slo_target=99.0)
        _checks(session, monitor.id, total=1000, failed=1)

    assert cli.main(["slo", "--config", config_path, "--exit-code"]) == 0


def test_the_command_exits_one_on_a_missed_target_under_exit_code(tmp_path, capsys):
    config_path, database = _config(tmp_path)
    with database.session() as session:
        monitor = _monitor(session, "api", slo_target=99.0)
        _checks(session, monitor.id, total=1000, failed=100)

    assert cli.main(["slo", "--config", config_path, "--exit-code"]) == 1


def test_a_budget_merely_running_low_does_not_fail_the_gate(tmp_path, capsys):
    """An at-risk service is the one a deploy might be needed to fix."""
    config_path, database = _config(tmp_path)
    with database.session() as session:
        monitor = _monitor(session, "api", slo_target=99.0)
        _checks(session, monitor.id, total=1000, failed=8)

    exit_code = cli.main(["slo", "--config", config_path, "--exit-code"])

    assert "AT RISK" in capsys.readouterr().out
    assert exit_code == 0


def test_the_command_without_exit_code_reports_a_breach_but_exits_zero(tmp_path, capsys):
    config_path, database = _config(tmp_path)
    with database.session() as session:
        monitor = _monitor(session, "api", slo_target=99.0)
        _checks(session, monitor.id, total=1000, failed=100)

    exit_code = cli.main(["slo", "--config", config_path])

    assert "BREACHED" in capsys.readouterr().out
    assert exit_code == 0


def test_a_filter_matching_nothing_is_reported_rather_than_shown_empty(tmp_path, capsys):
    config_path, database = _config(tmp_path)
    with database.session() as session:
        _monitor(session, "api", slo_target=99.0, tags=["internal"])

    exit_code = cli.main(["slo", "--config", config_path, "--tag", "prod"])

    output = capsys.readouterr().out
    assert "No monitor tagged 'prod' has an SLO target" in output
    assert exit_code == 0


def test_a_filter_matching_nothing_fails_the_gate(tmp_path, capsys):
    """A gate that passes because a tag was mistyped fails unnoticed."""
    config_path, database = _config(tmp_path)
    with database.session() as session:
        _monitor(session, "api", slo_target=99.0, tags=["internal"])

    assert cli.main(["slo", "--config", config_path, "--tag", "prod", "--exit-code"]) == 1


def test_an_estate_with_no_objectives_at_all_says_so(tmp_path, capsys):
    config_path, database = _config(tmp_path)
    with database.session() as session:
        _monitor(session, "api", slo_target=None)

    exit_code = cli.main(["slo", "--config", config_path])

    assert "No monitors have an SLO target." in capsys.readouterr().out
    assert exit_code == 0


def test_a_paused_monitor_does_not_fail_the_gate(tmp_path, capsys):
    config_path, database = _config(tmp_path)
    with database.session() as session:
        monitor = _monitor(session, "api", slo_target=99.0, enabled=False)
        _checks(session, monitor.id, total=1000, failed=500)

    exit_code = cli.main(["slo", "--config", config_path, "--exit-code"])

    assert "paused, not in config" in capsys.readouterr().out
    assert exit_code == 0


# --- an estate that has not been synced ------------------------------------


def _config_with(tmp_path, monitors: str) -> tuple:
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "test.db"
    config_file.write_text(
        f"database:\n  url: sqlite:///{db_path}\n{monitors}"
    )
    database = Database(f"sqlite:///{db_path}")
    database.create_all()
    return str(config_file), database


_WITH_TARGET = """
monitors:
  - name: api
    type: http
    target: https://example.com
    interval_seconds: 60
    slo_target: 99.9
"""

_WITHOUT_TARGET = """
monitors:
  - name: api
    type: http
    target: https://example.com
    interval_seconds: 60
"""


def test_an_unsynced_config_with_targets_says_so_rather_than_denying_them(
    tmp_path, capsys
):
    """The config in hand declares a target, so denying it would be false.

    Someone adding slo_target and running the report before restarting the
    scheduler would otherwise be told the target doesn't exist, and would go
    and check the line they had only just written.
    """
    config_path, _ = _config_with(tmp_path, _WITH_TARGET)

    exit_code = cli.main(["slo", "--config", config_path])

    output = capsys.readouterr().out
    assert "No monitors in the database yet" in output
    assert "Run `alerts run`" in output
    assert exit_code == 0


def test_an_unsynced_config_without_targets_gets_the_ordinary_message(
    tmp_path, capsys
):
    """Nothing has been promised, which is what the empty table already says."""
    config_path, _ = _config_with(tmp_path, _WITHOUT_TARGET)

    cli.main(["slo", "--config", config_path])

    assert "No monitors have an SLO target." in capsys.readouterr().out


def test_a_synced_estate_with_no_targets_gets_the_ordinary_message(tmp_path, capsys):
    """The database has been synced, so there is nothing to explain."""
    config_path, database = _config_with(tmp_path, _WITH_TARGET)
    with database.session() as session:
        _monitor(session, "api", slo_target=None)

    cli.main(["slo", "--config", config_path])

    assert "No monitors have an SLO target." in capsys.readouterr().out


def test_the_unsynced_message_does_not_fail_the_gate(tmp_path, capsys):
    """Nothing has been measured, which is not a missed target."""
    config_path, _ = _config_with(tmp_path, _WITH_TARGET)

    assert cli.main(["slo", "--config", config_path, "--exit-code"]) == 0
