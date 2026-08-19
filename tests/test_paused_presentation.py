"""How a paused monitor -- one the config no longer declares -- is presented.

Its stored state is a frozen reading rather than a current one, so every
surface has to distinguish it from a monitor that is genuinely up or down
right now.
"""
import json
from datetime import datetime, timedelta, timezone

from alerts.models import Incident, IncidentState, Monitor, MonitorState, MonitorType
from alerts.reporting import (
    StatusRow,
    collect_status_rows,
    format_status_summary,
    format_status_table,
)
from alerts.web import render
from alerts.web.app import DashboardApp
from alerts.web.stats import UptimeStats, summarize_states

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)

_NO_STATS = UptimeStats(
    total_checks=0, successful_checks=0, uptime_percentage=None, average_latency_ms=None
)


def _monitor(name: str = "site", **overrides) -> Monitor:
    defaults = dict(
        name=name,
        type=MonitorType.HTTP,
        target="https://example.com",
        state=MonitorState.UP,
    )
    defaults.update(overrides)
    return Monitor(**defaults)


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


def _get(app, path: str):
    status = []
    body = b"".join(
        app({"REQUEST_METHOD": "GET", "PATH_INFO": path}, lambda s, h: status.append(s))
    )
    return status[0], body.decode("utf-8")


def test_a_transient_monitor_is_not_paused():
    """Column defaults land at flush time, so an unsaved row must not read as paused."""
    assert _monitor().is_paused is False


def test_an_enabled_monitor_is_not_paused():
    assert _monitor(enabled=True).is_paused is False


def test_a_disabled_monitor_is_paused():
    assert _monitor(enabled=False).is_paused is True


def test_summary_counts_paused_monitors_separately():
    summary = summarize_states(
        [
            _monitor("a", state=MonitorState.UP),
            _monitor("b", state=MonitorState.DOWN, enabled=False),
        ]
    )

    assert summary.total == 1, "the paused monitor is not part of the live total"
    assert summary.counts[MonitorState.DOWN] == 0
    assert summary.paused == 1


def test_all_up_ignores_a_paused_monitor_in_a_bad_state():
    summary = summarize_states(
        [
            _monitor("a", state=MonitorState.UP),
            _monitor("b", state=MonitorState.DOWN, enabled=False),
        ]
    )

    assert summary.all_up is True


def test_index_marks_a_paused_row():
    html = render.render_index([(_monitor(enabled=False), _NO_STATS, None)])

    assert "paused" in html


def test_index_does_not_mark_an_active_row():
    html = render.render_index([(_monitor(enabled=True), _NO_STATS, None)])

    assert "paused" not in html


def test_summary_tile_only_appears_when_something_is_paused():
    active_only = summarize_states([_monitor("a", state=MonitorState.UP)])
    assert "paused" not in render.render_summary(active_only)

    with_paused = summarize_states([_monitor("a", enabled=False)])
    assert "paused" in render.render_summary(with_paused)


def test_detail_page_explains_that_nothing_is_updating():
    html = render.render_monitor_detail(_monitor(enabled=False), _NO_STATS, [], [])

    assert "no longer in the config" in html


def test_status_table_notes_a_paused_monitor():
    table = format_status_table([_row(paused=True)], now=NOW)

    assert "paused, not in config" in table


def test_a_paused_note_wins_over_an_open_incident():
    """The incident is history the moment nothing is checking the monitor."""
    table = format_status_table(
        [_row(paused=True, open_incident_since=NOW - timedelta(hours=3))], now=NOW
    )

    assert "paused, not in config" in table
    assert "open incident" not in table


def test_status_summary_tallies_paused_separately():
    summary = format_status_summary(
        [_row(state=MonitorState.UP), _row(state=MonitorState.DOWN, paused=True)]
    )

    assert summary == "2 monitor(s): 1 up, 1 paused"


def test_a_paused_monitor_does_not_fail_the_exit_code():
    assert _row(state=MonitorState.DOWN, paused=True).is_down is False
    assert _row(state=MonitorState.DOWN).is_down is True


def test_collect_status_rows_reads_the_paused_flag(db):
    with db.session() as session:
        session.add(_monitor("live", enabled=True))
        session.add(_monitor("dropped", enabled=False))

    with db.session() as session:
        rows = {row.name: row for row in collect_status_rows(session)}

    assert rows["live"].paused is False
    assert rows["dropped"].paused is True


def test_api_monitors_reports_the_paused_flag(db):
    with db.session() as session:
        session.add(_monitor("dropped", enabled=False))

    status, body = _get(DashboardApp(db), "/api/monitors")

    assert status == "200 OK"
    assert json.loads(body)["monitors"][0]["paused"] is True


def test_api_summary_reports_the_paused_count(db):
    with db.session() as session:
        session.add(_monitor("live", enabled=True))
        session.add(_monitor("dropped", state=MonitorState.DOWN, enabled=False))

    status, body = _get(DashboardApp(db), "/api/summary")
    payload = json.loads(body)

    assert status == "200 OK"
    assert payload["paused"] == 1
    assert payload["total"] == 1
    assert payload["states"]["down"] == 0


def test_api_monitor_detail_reports_the_paused_flag(db):
    with db.session() as session:
        session.add(_monitor("dropped", enabled=False))
        session.flush()

    status, body = _get(DashboardApp(db), "/api/monitors/dropped")

    assert status == "200 OK"
    assert json.loads(body)["paused"] is True


def test_open_incidents_are_still_counted_for_active_monitors(db):
    with db.session() as session:
        monitor = _monitor("live", state=MonitorState.DOWN)
        session.add(monitor)
        session.flush()
        session.add(Incident(monitor_id=monitor.id, state=IncidentState.OPEN))

    status, body = _get(DashboardApp(db), "/api/summary")

    assert status == "200 OK"
    assert json.loads(body)["open_incidents"] == 1
