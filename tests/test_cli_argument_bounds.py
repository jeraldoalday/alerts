"""Range checking on the CLI's numeric window options.

Each of these ends up as a SQL `LIMIT` or as an operand of date arithmetic,
neither of which can object to a number that makes no sense. A rejected
argument is an operator seeing the range; an accepted one is a table that
looks like an answer.
"""
import pytest

from alerts import cli
from alerts.db import Database
from alerts.models import CheckResult, Monitor, MonitorState, MonitorType


def _config(tmp_path) -> str:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(f"database:\n  url: sqlite:///{tmp_path / 'test.db'}\n")
    return str(config_file)


def _seed_checks(tmp_path, count: int = 5) -> None:
    database = Database(f"sqlite:///{tmp_path / 'test.db'}")
    database.create_all()
    with database.session() as session:
        monitor = Monitor(
            name="api",
            type=MonitorType.HTTP,
            target="https://example.com",
            state=MonitorState.UP,
        )
        session.add(monitor)
        session.flush()
        for index in range(count):
            session.add(
                CheckResult(monitor_id=monitor.id, success=True, latency_ms=100.0 + index)
            )


@pytest.mark.parametrize("value", ["0", "-1", "-50"])
def test_status_checks_window_must_be_positive(value):
    with pytest.raises(SystemExit) as exit_info:
        cli._parse_args(["status", "--checks", value])

    assert exit_info.value.code == 2


def test_status_checks_window_rejects_an_absurd_upper_bound():
    with pytest.raises(SystemExit):
        cli._parse_args(["status", "--checks", "10001"])


def test_status_checks_window_accepts_the_range_ends():
    assert cli._parse_args(["status", "--checks", "1"]).checks == 1
    assert cli._parse_args(["status", "--checks", "10000"]).checks == 10_000


@pytest.mark.parametrize("value", ["0", "-1"])
def test_incident_limit_must_be_positive(value):
    with pytest.raises(SystemExit):
        cli._parse_args(["incidents", "--limit", value])


def test_incident_limit_is_capped():
    with pytest.raises(SystemExit):
        cli._parse_args(["incidents", "--limit", "501"])

    assert cli._parse_args(["incidents", "--limit", "500"]).limit == 500


def test_incident_days_may_be_zero_but_not_negative():
    """0 is the documented "everything still kept"; a negative one is a typo."""
    assert cli._parse_args(["incidents", "--days", "0"]).days == 0

    with pytest.raises(SystemExit):
        cli._parse_args(["incidents", "--days", "-1"])


def test_incident_days_is_capped():
    with pytest.raises(SystemExit):
        cli._parse_args(["incidents", "--days", "3651"])


@pytest.mark.parametrize("value", ["", "seven", "1.5", "1e3"])
def test_a_non_integer_is_refused_with_the_argument_named(value, capsys):
    with pytest.raises(SystemExit):
        cli._parse_args(["status", "--checks", value])

    assert "--checks" in capsys.readouterr().err


def test_the_message_names_the_range_rather_than_just_failing(capsys):
    with pytest.raises(SystemExit):
        cli._parse_args(["status", "--checks", "0"])

    error = capsys.readouterr().err
    assert "1 to 10000" in error


def test_a_zero_checks_window_used_to_report_a_live_monitor_as_never_checked(
    tmp_path, capsys
):
    """The behaviour the bound exists to prevent, and the one it now gives.

    The monitor has five checks seconds old. With the window accepted as
    typed, the table said "never" in the LAST CHECK column and "-" in every
    figure -- a report indistinguishable from a monitor nothing has ever
    checked.
    """
    _seed_checks(tmp_path)
    config_path = _config(tmp_path)

    with pytest.raises(SystemExit):
        cli.main(["status", "--config", config_path, "--checks", "0"])

    assert "never" not in capsys.readouterr().out

    assert cli.main(["status", "--config", config_path, "--checks", "5"]) == 0
    assert "100.0%" in capsys.readouterr().out


def test_a_negative_checks_window_never_reaches_the_query(tmp_path, capsys):
    """`LIMIT -1` is "no limit" in SQLite, so -1 widened the window it narrowed.

    Seeded with more checks than any window an operator would ask for, so an
    accepted -1 would show 100% over all 200 of them rather than over the 5
    that were asked for.
    """
    _seed_checks(tmp_path, count=200)

    with pytest.raises(SystemExit):
        cli.main(["status", "--config", _config(tmp_path), "--checks", "-1"])

    assert "100.0%" not in capsys.readouterr().out
