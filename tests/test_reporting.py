"""Status table rendering and the rows it's built from."""
from datetime import datetime, timedelta, timezone

from alerts.models import CheckResult, Incident, IncidentState, Monitor, MonitorState, MonitorType
from alerts.reporting import (
    StatusRow,
    collect_status_rows,
    format_duration,
    format_status_summary,
    format_status_table,
)

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)


def _row(**overrides) -> StatusRow:
    defaults = dict(
        name="site",
        type="http",
        state=MonitorState.UP,
        uptime_percentage=99.5,
        average_latency_ms=42.0,
        last_checked_at=NOW - timedelta(seconds=30),
        open_incident_since=None,
    )
    defaults.update(overrides)
    return StatusRow(**defaults)


def _monitor(session, name: str, **overrides) -> Monitor:
    defaults = dict(name=name, type=MonitorType.HTTP, target=f"https://{name}.example.com")
    defaults.update(overrides)
    monitor = Monitor(**defaults)
    session.add(monitor)
    session.flush()
    return monitor


def test_format_duration_units():
    assert format_duration(45) == "45s"
    assert format_duration(90) == "1m"
    assert format_duration(3_700) == "1h 1m"
    assert format_duration(90_000) == "1d 1h"
    assert format_duration(-5) == "0s", "a clock skew shouldn't render a negative age"


def test_table_has_a_header_and_one_line_per_monitor():
    output = format_status_table([_row(name="a"), _row(name="b")], now=NOW)

    lines = output.splitlines()
    assert lines[0].split() == [
        "MONITOR",
        "TYPE",
        "TAGS",
        "STATE",
        "UPTIME",
        "LATENCY",
        "P95",
        "LAST",
        "CHECK",
        "NOTE",
    ]
    assert len(lines) == 3


def test_table_columns_are_aligned_to_the_widest_cell():
    output = format_status_table([_row(name="short"), _row(name="a-much-longer-name")], now=NOW)

    lines = output.splitlines()[1:]
    assert len({line.index("http") for line in lines}) == 1


def test_table_formats_uptime_latency_and_check_age():
    output = format_status_table(
        [_row(uptime_percentage=99.44, average_latency_ms=42.6)], now=NOW
    )

    assert "99.4%" in output
    assert "43ms" in output
    assert "30s ago" in output


def test_table_renders_missing_stats_as_dashes():
    output = format_status_table(
        [_row(uptime_percentage=None, average_latency_ms=None, last_checked_at=None)], now=NOW
    )

    assert "never" in output
    assert " - " in output


def test_table_notes_how_long_an_incident_has_been_open():
    output = format_status_table(
        [
            _row(
                state=MonitorState.DOWN,
                open_incident_since=NOW - timedelta(hours=2, minutes=5),
            )
        ],
        now=NOW,
    )

    assert "open incident, 2h 5m" in output


def test_table_lines_have_no_trailing_whitespace():
    output = format_status_table([_row()], now=NOW)

    assert all(line == line.rstrip() for line in output.splitlines())


def test_empty_table_says_so():
    assert format_status_table([], now=NOW) == "No monitors configured."


def test_summary_tallies_states_present():
    rows = [
        _row(name="a", state=MonitorState.UP),
        _row(name="b", state=MonitorState.UP),
        _row(name="c", state=MonitorState.DOWN),
    ]

    summary = format_status_summary(rows)

    assert summary == "3 monitor(s): 2 up, 1 down"


def test_summary_of_no_monitors():
    assert format_status_summary([]) == "0 monitor(s)"


def test_is_down_only_for_the_down_state():
    assert _row(state=MonitorState.DOWN).is_down is True
    assert _row(state=MonitorState.DEGRADED).is_down is False


def test_collect_status_rows_reads_state_stats_and_open_incidents(db):
    with db.session() as session:
        healthy = _monitor(session, "healthy", state=MonitorState.UP)
        broken = _monitor(session, "broken", state=MonitorState.DOWN)
        session.add(CheckResult(monitor_id=healthy.id, success=True, latency_ms=20.0))
        session.add(CheckResult(monitor_id=broken.id, success=False))
        session.add(Incident(monitor_id=broken.id, state=IncidentState.OPEN))

    with db.session() as session:
        rows = collect_status_rows(session)

    by_name = {row.name: row for row in rows}
    assert [row.name for row in rows] == ["broken", "healthy"], "sorted by name"
    assert by_name["healthy"].uptime_percentage == 100.0
    assert by_name["healthy"].average_latency_ms == 20.0
    assert by_name["healthy"].open_incident_since is None
    assert by_name["broken"].uptime_percentage == 0.0
    assert by_name["broken"].open_incident_since is not None


def test_collect_status_rows_respects_the_checks_window(db):
    with db.session() as session:
        monitor = _monitor(session, "site")
        session.add(CheckResult(monitor_id=monitor.id, success=False))
        session.flush()
        session.add(CheckResult(monitor_id=monitor.id, success=True))

    with db.session() as session:
        assert collect_status_rows(session, checks_window=1)[0].uptime_percentage == 100.0
        assert collect_status_rows(session, checks_window=2)[0].uptime_percentage == 50.0


def test_collect_status_rows_ignores_resolved_incidents(db):
    with db.session() as session:
        monitor = _monitor(session, "site")
        session.add(
            Incident(monitor_id=monitor.id, state=IncidentState.RESOLVED, resolved_at=NOW)
        )

    with db.session() as session:
        assert collect_status_rows(session)[0].open_incident_since is None


def test_the_table_renders_the_p95_column():
    output = format_status_table([_row(average_latency_ms=484.0, p95_latency_ms=4000.0)], now=NOW)

    assert "484ms" in output
    assert "4000ms" in output


def test_a_row_with_no_p95_renders_a_dash():
    output = format_status_table([_row(p95_latency_ms=None)], now=NOW)

    assert output.splitlines()[1].split()[5] == "-"


def test_collect_status_rows_fills_in_the_p95(db):
    """The same window the mean is taken over, so the two can't disagree."""
    with db.session() as session:
        monitor = _monitor(session, "site")
        for latency in [90.0] * 19 + [4000.0]:
            session.add(
                CheckResult(monitor_id=monitor.id, success=True, latency_ms=latency)
            )

    with db.session() as session:
        row = collect_status_rows(session)[0]

    assert row.p95_latency_ms == 90.0
    assert row.average_latency_ms == 285.5


def test_a_monitor_with_no_checks_has_no_p95(db):
    with db.session() as session:
        _monitor(session, "site")

    with db.session() as session:
        assert collect_status_rows(session)[0].p95_latency_ms is None
