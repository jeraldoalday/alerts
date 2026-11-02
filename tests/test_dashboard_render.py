from datetime import datetime, timezone

from alerts.models import (
    CheckResult,
    Incident,
    IncidentState,
    Monitor,
    MonitorState,
    MonitorType,
)
from alerts.web import render
from alerts.web.stats import UptimeStats

_NO_STATS = UptimeStats(
    total_checks=0, successful_checks=0, uptime_percentage=None, average_latency_ms=None
)


def _monitor(**overrides) -> Monitor:
    defaults = dict(
        name="site",
        type=MonitorType.HTTP,
        target="https://example.com",
        state=MonitorState.UP,
    )
    defaults.update(overrides)
    return Monitor(**defaults)


def test_render_index_lists_monitor_name_as_link():
    html = render.render_index([(_monitor(), _NO_STATS, None)])

    assert '<a href="/monitors/site">site</a>' in html


def test_render_index_handles_no_monitors():
    html = render.render_index([])

    assert "No monitors configured." in html


def test_render_index_shows_uptime_percentage():
    stats = UptimeStats(
        total_checks=4, successful_checks=3, uptime_percentage=75.0, average_latency_ms=42.0
    )

    html = render.render_index([(_monitor(), stats, None)])

    assert "75.0%" in html
    assert "42 ms" in html


def test_render_index_escapes_monitor_name():
    monitor = _monitor(name="a<script>evil()</script>")

    html = render.render_index([(monitor, _NO_STATS, None)])

    assert "<script>evil()</script>" not in html
    assert "&lt;script&gt;" in html


def test_render_index_declares_language_and_viewport():
    """A screen reader needs `lang` to pick the right voice, and a phone
    needs the viewport meta to avoid rendering the dashboard at desktop
    width and then zooming it down to illegible."""
    html = render.render_index([(_monitor(), _NO_STATS, None)])

    assert '<html lang="en">' in html
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html


def test_render_index_table_headers_declare_column_scope():
    """`scope="col"` is what lets a screen reader announce "Monitor" when
    it reads the first cell of a data row, instead of just the cell text."""
    html = render.render_index([(_monitor(), _NO_STATS, None)])

    assert '<th scope="col">Monitor</th>' in html


def test_render_monitor_detail_escapes_error_message():
    monitor = _monitor()
    check = CheckResult(
        monitor_id=1,
        success=False,
        checked_at=datetime.now(timezone.utc),
        error_message="<img src=x onerror=alert(1)>",
    )

    html = render.render_monitor_detail(monitor, _NO_STATS, [check], [])

    assert "<img src=x onerror=alert(1)>" not in html
    assert "&lt;img" in html


def test_render_monitor_detail_shows_incident_cause():
    monitor = _monitor()
    incident = Incident(
        monitor_id=1,
        state=IncidentState.OPEN,
        cause="connection refused",
        started_at=datetime.now(timezone.utc),
    )

    html = render.render_monitor_detail(monitor, _NO_STATS, [], [incident])

    assert "connection refused" in html
    assert "open" in html


def test_render_monitor_detail_handles_no_checks_or_incidents():
    html = render.render_monitor_detail(_monitor(), _NO_STATS, [], [])

    assert "No checks yet." in html
    assert "No incidents." in html


def test_render_not_found_includes_message():
    html = render.render_not_found("monitor 'ghost' not found")

    assert "monitor &#x27;ghost&#x27; not found" in html


def test_detail_page_names_the_latency_budget_behind_a_degraded_badge():
    monitor = _monitor(state=MonitorState.DEGRADED, degraded_latency_ms=1500.0)

    html = render.render_monitor_detail(monitor, _NO_STATS, [], [])

    assert "degraded above 1500 ms" in html


def test_detail_page_omits_the_budget_when_the_monitor_has_none():
    html = render.render_monitor_detail(_monitor(), _NO_STATS, [], [])

    assert "degraded above" not in html
