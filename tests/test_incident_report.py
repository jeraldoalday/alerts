"""The `alerts incidents` report: row collection, rendering, and the subcommand.

`alerts status` is a snapshot of now; this is the history behind it. The
cases that matter most are the ones where an incident is still open, since
it has no end to measure against and every figure has to decide what to do
about that.
"""
from datetime import datetime, timedelta

from alerts import cli
from alerts.db import Database
from alerts.models import Incident, IncidentState, Monitor, MonitorState, MonitorType
from alerts.reporting import (
    IncidentRow,
    collect_incident_rows,
    format_incident_summary,
    format_incident_table,
)
from alerts.timeutil import as_utc_naive, utcnow

# The fixed "now" the row-collection and rendering tests measure against.
# Every one of them passes it in explicitly, so their incidents can sit at
# a date chosen for readability and stay where they were put.
NOW = datetime(2026, 8, 21, 12, 0, 0)


def _clock_now() -> datetime:
    """What the subcommand-level tests anchor their incidents to.

    `alerts incidents` measures its window against the real clock, so an
    incident written two hours before a date frozen into this file drifts
    out of the default seven-day window as soon as the file is a week old
    -- and the test then fails on a Tuesday for reasons that have nothing
    to do with the code it covers. Anchoring on the same clock the command
    reads keeps "two hours ago" meaning two hours ago.
    """
    return as_utc_naive(utcnow())


def _monitor(session, name: str = "site", *, tags=None) -> Monitor:
    monitor = Monitor(
        name=name,
        type=MonitorType.HTTP,
        target="https://example.com",
        state=MonitorState.UP,
        tags=list(tags) if tags is not None else [],
    )
    session.add(monitor)
    session.flush()
    return monitor


def _incident(
    session, monitor_id: int, *, started_ago, lasted=None, cause="boom", anchor=None
) -> Incident:
    started_at = (anchor or NOW) - started_ago
    resolved_at = started_at + lasted if lasted is not None else None
    incident = Incident(
        monitor_id=monitor_id,
        state=IncidentState.RESOLVED if lasted is not None else IncidentState.OPEN,
        started_at=started_at,
        resolved_at=resolved_at,
        cause=cause,
    )
    session.add(incident)
    return incident


def _row(**overrides) -> IncidentRow:
    defaults = dict(
        monitor="site",
        state=IncidentState.RESOLVED,
        started_at=NOW - timedelta(hours=2),
        resolved_at=NOW - timedelta(hours=1),
        cause="boom",
    )
    defaults.update(overrides)
    return IncidentRow(**defaults)


# --- row collection --------------------------------------------------------


def test_no_incidents_collects_no_rows(db):
    with db.session() as session:
        assert collect_incident_rows(session, now=NOW) == []


def test_rows_carry_the_monitor_name(db):
    with db.session() as session:
        monitor = _monitor(session, "api-gateway")
        _incident(session, monitor.id, started_ago=timedelta(hours=3), lasted=timedelta(minutes=8))

    with db.session() as session:
        rows = collect_incident_rows(session, now=NOW)

    assert [row.monitor for row in rows] == ["api-gateway"]


def test_rows_are_newest_first(db):
    with db.session() as session:
        monitor = _monitor(session)
        _incident(session, monitor.id, started_ago=timedelta(days=2), lasted=timedelta(minutes=1))
        _incident(session, monitor.id, started_ago=timedelta(hours=1), lasted=timedelta(minutes=1))

    with db.session() as session:
        rows = collect_incident_rows(session, now=NOW)

    assert rows[0].started_at > rows[1].started_at


def test_the_window_excludes_older_incidents(db):
    with db.session() as session:
        monitor = _monitor(session)
        _incident(session, monitor.id, started_ago=timedelta(days=30), lasted=timedelta(minutes=1))
        _incident(session, monitor.id, started_ago=timedelta(days=2), lasted=timedelta(minutes=1))

    with db.session() as session:
        rows = collect_incident_rows(session, since_days=7, now=NOW)

    assert len(rows) == 1


def test_no_window_reports_everything_kept(db):
    with db.session() as session:
        monitor = _monitor(session)
        _incident(session, monitor.id, started_ago=timedelta(days=300), lasted=timedelta(minutes=1))

    with db.session() as session:
        rows = collect_incident_rows(session, since_days=None, now=NOW)

    assert len(rows) == 1


def test_an_open_incident_older_than_the_window_is_still_reported(db):
    """A three-week outage must not fall out of a seven-day report.

    Dropping it would answer "nothing has been broken lately" about a
    service that is broken right now.
    """
    with db.session() as session:
        monitor = _monitor(session)
        _incident(session, monitor.id, started_ago=timedelta(days=21))

    with db.session() as session:
        rows = collect_incident_rows(session, since_days=7, now=NOW)

    assert len(rows) == 1
    assert rows[0].is_open is True


def test_a_resolved_incident_older_than_the_window_is_not_reported(db):
    """The exemption is for outages still happening, not for old ones."""
    with db.session() as session:
        monitor = _monitor(session)
        _incident(session, monitor.id, started_ago=timedelta(days=21), lasted=timedelta(hours=1))

    with db.session() as session:
        assert collect_incident_rows(session, since_days=7, now=NOW) == []


def test_the_monitor_filter_narrows_to_one_service(db):
    with db.session() as session:
        first = _monitor(session, "api")
        second = _monitor(session, "site")
        _incident(session, first.id, started_ago=timedelta(hours=1), lasted=timedelta(minutes=1))
        _incident(session, second.id, started_ago=timedelta(hours=2), lasted=timedelta(minutes=1))

    with db.session() as session:
        rows = collect_incident_rows(session, monitor="api", now=NOW)

    assert [row.monitor for row in rows] == ["api"]


def test_an_unknown_monitor_name_reports_nothing_rather_than_erroring(db):
    with db.session() as session:
        monitor = _monitor(session)
        _incident(session, monitor.id, started_ago=timedelta(hours=1), lasted=timedelta(minutes=1))

    with db.session() as session:
        assert collect_incident_rows(session, monitor="typo", now=NOW) == []


def test_the_tag_filter_narrows_to_every_monitor_carrying_it(db):
    with db.session() as session:
        api = _monitor(session, "api", tags=["payments"])
        site = _monitor(session, "site", tags=["web"])
        _incident(session, api.id, started_ago=timedelta(hours=1), lasted=timedelta(minutes=1))
        _incident(session, site.id, started_ago=timedelta(hours=2), lasted=timedelta(minutes=1))

    with db.session() as session:
        rows = collect_incident_rows(session, tag="payments", now=NOW)

    assert [row.monitor for row in rows] == ["api"]


def test_an_unknown_tag_reports_nothing_rather_than_erroring(db):
    with db.session() as session:
        monitor = _monitor(session, tags=["prod"])
        _incident(session, monitor.id, started_ago=timedelta(hours=1), lasted=timedelta(minutes=1))

    with db.session() as session:
        assert collect_incident_rows(session, tag="staging", now=NOW) == []


def test_monitor_and_tag_narrow_together(db):
    with db.session() as session:
        api = _monitor(session, "api", tags=["payments"])
        site = _monitor(session, "site", tags=["payments"])
        _incident(session, api.id, started_ago=timedelta(hours=1), lasted=timedelta(minutes=1))
        _incident(session, site.id, started_ago=timedelta(hours=2), lasted=timedelta(minutes=1))

    with db.session() as session:
        rows = collect_incident_rows(session, monitor="api", tag="payments", now=NOW)

    assert [row.monitor for row in rows] == ["api"]


def test_a_monitor_not_carrying_the_given_tag_reports_nothing(db):
    """`--monitor api --tag payments` must not fall back to just `--monitor api`."""
    with db.session() as session:
        monitor = _monitor(session, "api", tags=["web"])
        _incident(session, monitor.id, started_ago=timedelta(hours=1), lasted=timedelta(minutes=1))

    with db.session() as session:
        assert collect_incident_rows(session, monitor="api", tag="payments", now=NOW) == []


def test_open_only_drops_the_resolved_ones(db):
    with db.session() as session:
        monitor = _monitor(session)
        _incident(session, monitor.id, started_ago=timedelta(hours=5), lasted=timedelta(minutes=3))
        _incident(session, monitor.id, started_ago=timedelta(hours=1))

    with db.session() as session:
        rows = collect_incident_rows(session, open_only=True, now=NOW)

    assert [row.is_open for row in rows] == [True]


def test_the_limit_caps_the_listed_incidents(db):
    with db.session() as session:
        monitor = _monitor(session)
        for hours in range(1, 6):
            _incident(
                session,
                monitor.id,
                started_ago=timedelta(hours=hours),
                lasted=timedelta(minutes=1),
            )

    with db.session() as session:
        rows = collect_incident_rows(session, limit=2, now=NOW)

    assert len(rows) == 2


# --- durations -------------------------------------------------------------


def test_a_resolved_incident_is_measured_between_its_ends():
    row = _row(started_at=NOW - timedelta(hours=3), resolved_at=NOW - timedelta(hours=1))

    assert row.duration_seconds(NOW) == 2 * 3600


def test_an_open_incident_is_measured_against_now():
    row = _row(state=IncidentState.OPEN, resolved_at=None, started_at=NOW - timedelta(minutes=30))

    assert row.duration_seconds(NOW) == 30 * 60


# --- table -----------------------------------------------------------------


def test_an_empty_report_says_so():
    assert format_incident_table([]) == "No incidents in the reporting window."


def test_the_table_lists_the_monitor_duration_and_cause():
    row = _row(monitor="api-gateway", cause="expected status 200, got 502")

    table = format_incident_table([row], now=NOW)

    assert "MONITOR" in table
    assert "api-gateway" in table
    assert "1h 0m" in table
    assert "expected status 200, got 502" in table


def test_an_open_incident_is_marked_as_still_running():
    row = _row(state=IncidentState.OPEN, resolved_at=None, started_at=NOW - timedelta(minutes=45))

    table = format_incident_table([row], now=NOW)

    assert "45m so far" in table
    assert "open" in table


def test_a_long_cause_is_truncated_to_keep_the_columns_aligned():
    row = _row(cause="x" * 200)

    table = format_incident_table([row], now=NOW)

    assert "x" * 200 not in table
    assert "…" in table
    assert max(len(line) for line in table.splitlines()) < 100


def test_a_missing_cause_leaves_the_column_empty():
    table = format_incident_table([_row(cause=None)], now=NOW)

    assert not table.splitlines()[1].endswith(" ")


def test_every_line_is_the_same_width_where_it_matters():
    rows = [_row(monitor="a"), _row(monitor="a-much-longer-monitor-name")]

    table = format_incident_table(rows, now=NOW)

    starts = [line.index("2026") for line in table.splitlines()[1:]]
    assert len(set(starts)) == 1, "the STARTED column lines up"


# --- summary ---------------------------------------------------------------


def test_an_empty_summary_counts_nothing():
    assert format_incident_summary([]) == "0 incident(s)"


def test_the_summary_totals_downtime_and_mean_recovery():
    rows = [
        _row(started_at=NOW - timedelta(hours=4), resolved_at=NOW - timedelta(hours=3)),
        _row(started_at=NOW - timedelta(hours=2), resolved_at=NOW - timedelta(hours=1)),
    ]

    summary = format_incident_summary(rows, now=NOW)

    assert "2 incident(s)" in summary
    assert "2h 0m total downtime" in summary
    assert "mean recovery 1h 0m" in summary


def test_the_summary_counts_open_incidents_separately():
    rows = [_row(state=IncidentState.OPEN, resolved_at=None, started_at=NOW - timedelta(hours=1))]

    summary = format_incident_summary(rows, now=NOW)

    assert "1 still open" in summary


def test_open_incidents_count_towards_downtime_but_not_mean_recovery():
    """An outage getting longer must not appear to improve the recovery time."""
    rows = [
        _row(started_at=NOW - timedelta(hours=2), resolved_at=NOW - timedelta(hours=1)),
        _row(state=IncidentState.OPEN, resolved_at=None, started_at=NOW - timedelta(hours=9)),
    ]

    summary = format_incident_summary(rows, now=NOW)

    assert "10h 0m total downtime" in summary
    assert "mean recovery 1h 0m" in summary


def test_a_report_of_only_open_incidents_has_no_mean_recovery():
    rows = [_row(state=IncidentState.OPEN, resolved_at=None, started_at=NOW - timedelta(hours=1))]

    assert "mean recovery" not in format_incident_summary(rows, now=NOW)


# --- the subcommand --------------------------------------------------------


def _config(tmp_path):
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "test.db"
    config_file.write_text(f"database:\n  url: sqlite:///{db_path}\n")
    return str(config_file), f"sqlite:///{db_path}"


def test_parse_args_incidents_defaults():
    args = cli._parse_args(["incidents"])

    assert args.command == "incidents"
    assert args.config == "config.yaml"
    assert args.monitor is None
    assert args.tag is None
    assert args.days == 7
    assert args.limit == 50
    assert args.open_only is False
    assert args.exit_code is False


def test_parse_args_incidents_accepts_every_filter():
    args = cli._parse_args(
        [
            "incidents",
            "--monitor",
            "api",
            "--tag",
            "payments",
            "--days",
            "30",
            "--limit",
            "5",
            "--open",
            "--exit-code",
        ]
    )

    assert args.monitor == "api"
    assert args.tag == "payments"
    assert args.days == 30
    assert args.limit == 5
    assert args.open_only is True
    assert args.exit_code is True


def test_main_incidents_filters_by_tag(tmp_path, capsys):
    config_path, database_url = _config(tmp_path)
    database = Database(database_url)
    database.create_all()
    with database.session() as session:
        api = _monitor(session, "api", tags=["payments"])
        site = _monitor(session, "site", tags=["web"])
        _incident(
            session,
            api.id,
            started_ago=timedelta(hours=1),
            lasted=timedelta(minutes=5),
            anchor=_clock_now(),
        )
        _incident(
            session,
            site.id,
            started_ago=timedelta(hours=2),
            lasted=timedelta(minutes=5),
            anchor=_clock_now(),
        )

    exit_code = cli.main(["incidents", "--config", config_path, "--tag", "payments"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "api" in output
    assert "site" not in output
    assert "1 incident(s)" in output


def test_main_incidents_prints_a_table_and_a_summary(tmp_path, capsys):
    config_path, database_url = _config(tmp_path)
    database = Database(database_url)
    database.create_all()
    with database.session() as session:
        monitor = _monitor(session, "api-gateway")
        _incident(
            session,
            monitor.id,
            started_ago=timedelta(hours=2),
            lasted=timedelta(minutes=42),
            cause="expected status 200, got 502",
            anchor=_clock_now(),
        )

    exit_code = cli.main(["incidents", "--config", config_path])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "MONITOR" in output
    assert "api-gateway" in output
    assert "1 incident(s)" in output


def test_main_incidents_on_an_empty_database(tmp_path, capsys):
    config_path, _ = _config(tmp_path)

    assert cli.main(["incidents", "--config", config_path]) == 0
    assert "No incidents in the reporting window." in capsys.readouterr().out


def test_main_incidents_exit_code_flag_reports_an_open_incident(tmp_path, capsys):
    config_path, database_url = _config(tmp_path)
    database = Database(database_url)
    database.create_all()
    with database.session() as session:
        monitor = _monitor(session)
        _incident(session, monitor.id, started_ago=timedelta(minutes=5), anchor=_clock_now())

    assert cli.main(["incidents", "--config", config_path]) == 0, "non-zero exit is opt-in"
    assert cli.main(["incidents", "--config", config_path, "--exit-code"]) == 1
    capsys.readouterr()


def test_main_incidents_exit_code_is_zero_when_everything_recovered(tmp_path, capsys):
    config_path, database_url = _config(tmp_path)
    database = Database(database_url)
    database.create_all()
    with database.session() as session:
        monitor = _monitor(session)
        _incident(
            session,
            monitor.id,
            started_ago=timedelta(hours=1),
            lasted=timedelta(minutes=2),
            anchor=_clock_now(),
        )

    assert cli.main(["incidents", "--config", config_path, "--exit-code"]) == 0
    capsys.readouterr()


def test_main_incidents_days_zero_drops_the_window(tmp_path, capsys):
    config_path, database_url = _config(tmp_path)
    database = Database(database_url)
    database.create_all()
    with database.session() as session:
        monitor = _monitor(session)
        _incident(
            session,
            monitor.id,
            started_ago=timedelta(days=200),
            lasted=timedelta(minutes=1),
            anchor=_clock_now(),
        )

    cli.main(["incidents", "--config", config_path, "--days", "0"])
    unbounded = capsys.readouterr().out
    cli.main(["incidents", "--config", config_path])
    default_window = capsys.readouterr().out

    assert "1 incident(s)" in unbounded
    assert "0 incident(s)" in default_window
