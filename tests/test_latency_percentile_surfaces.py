"""Latency percentiles as they reach the dashboard, the API, and /metrics.

`test_latency_percentiles.py` pins the arithmetic; this pins the presentation
-- that the same window of checks produces the same figures wherever it is
rendered, and that an absent percentile is shown as absent rather than as
zero.
"""
import json

from alerts.models import CheckResult, Monitor, MonitorState, MonitorType
from alerts.web import render
from alerts.web.app import DashboardApp
from alerts.web.metrics import parse_sample, render_metrics
from alerts.web.stats import UptimeStats, compute_uptime_stats

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


def _persisted_monitor(session, **overrides) -> Monitor:
    monitor = _monitor(**overrides)
    session.add(monitor)
    session.flush()
    return monitor


def _stats(*latencies) -> UptimeStats:
    return compute_uptime_stats(
        [CheckResult(success=True, latency_ms=latency) for latency in latencies]
    )


def _call(app: DashboardApp, path: str):
    captured = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = dict(headers)

    body = b"".join(
        app({"REQUEST_METHOD": "GET", "PATH_INFO": path, "QUERY_STRING": ""}, start_response)
    )
    return captured["status"], captured["headers"], body


def _samples(text: str, name: str):
    parsed = [parse_sample(line) for line in text.splitlines()]
    return [p for p in parsed if p is not None and p[0] == name]


def _seed_checks(session, monitor_id: int, latencies) -> None:
    for latency in latencies:
        session.add(
            CheckResult(monitor_id=monitor_id, success=True, latency_ms=latency, status_code=200)
        )


# --- HTML ------------------------------------------------------------------


def test_the_index_shows_p95_next_to_the_mean():
    stats = _stats(*([100.0] * 19 + [4000.0]))

    html = render.render_index([(_monitor(), stats, None)])

    assert '<th scope="col">p95 latency</th>' in html
    assert "295 ms" in html, "the mean, which the outlier barely moves"
    assert "100 ms" in html, "p95, which is not the outlier either"


def test_the_index_row_still_matches_its_header_width():
    """The empty-state row spans the table, so its colspan tracks the columns."""
    html = render.render_index([])

    assert html.count('<th scope="col">') == 9
    assert 'colspan="9"' in html


def test_the_detail_page_lists_every_percentile():
    stats = _stats(*[float(n) for n in range(1, 101)])

    html = render.render_monitor_detail(_monitor(), stats, [], [])

    assert "p50 50 ms" in html
    assert "p95 95 ms" in html
    assert "p99 99 ms" in html


def test_the_detail_page_omits_the_percentile_line_with_no_latencies():
    html = render.render_monitor_detail(_monitor(), _NO_STATS, [], [])

    assert "p50" not in html


def test_a_monitor_with_no_latency_shows_a_dash_not_a_zero_on_the_index():
    """A p95 of "0 ms" would read as the fastest service on the board."""
    stats = compute_uptime_stats([CheckResult(success=False, latency_ms=None)])

    html = render.render_index([(_monitor(), stats, None)])

    assert "0 ms" not in html


# --- JSON API --------------------------------------------------------------


def test_api_monitors_carries_the_percentiles(db):
    with db.session() as session:
        monitor = _persisted_monitor(session)
        _seed_checks(session, monitor.id, [10.0, 20.0, 30.0, 40.0])

    status, _, body = _call(DashboardApp(db), "/api/monitors")

    assert status == "200 OK"
    payload = json.loads(body)["monitors"][0]
    assert payload["latency_percentiles"] == {"p50": 20.0, "p95": 40.0, "p99": 40.0}


def test_api_monitor_detail_carries_the_percentiles(db):
    with db.session() as session:
        monitor = _persisted_monitor(session)
        _seed_checks(session, monitor.id, [10.0, 20.0, 30.0, 40.0])

    status, _, body = _call(DashboardApp(db), "/api/monitors/site")

    assert status == "200 OK"
    assert json.loads(body)["latency_percentiles"]["p95"] == 40.0


def test_api_percentiles_are_null_when_nothing_recorded_a_latency(db):
    with db.session() as session:
        monitor = _persisted_monitor(session)
        session.add(CheckResult(monitor_id=monitor.id, success=False, latency_ms=None))

    _, _, body = _call(DashboardApp(db), "/api/monitors")

    payload = json.loads(body)["monitors"][0]
    assert payload["latency_percentiles"] == {"p50": None, "p95": None, "p99": None}


def test_the_api_and_the_dashboard_agree_on_p95(db):
    """Both read the same window through the same aggregation."""
    with db.session() as session:
        monitor = _persisted_monitor(session)
        _seed_checks(session, monitor.id, [50.0, 150.0, 250.0, 350.0])

    _, _, api_body = _call(DashboardApp(db), "/api/monitors")
    _, _, html_body = _call(DashboardApp(db), "/")

    p95 = json.loads(api_body)["monitors"][0]["latency_percentiles"]["p95"]
    assert p95 == 350.0
    assert f"{p95:.0f} ms".encode() in html_body


# --- metrics ---------------------------------------------------------------


def test_metrics_exports_one_sample_per_quantile():
    text = render_metrics([(_monitor(), _stats(*[float(n) for n in range(1, 101)]))])

    samples = _samples(text, "alerts_monitor_latency_quantile_ms")
    assert {labels["quantile"]: value for _, labels, value in samples} == {
        "0.5": "50",
        "0.95": "95",
        "0.99": "99",
    }


def test_the_quantile_label_is_a_fraction_not_a_percentage():
    """Dashboards written against other exporters filter on quantile="0.95"."""
    text = render_metrics([(_monitor(), _stats(10.0))])

    samples = _samples(text, "alerts_monitor_latency_quantile_ms")
    quantiles = {labels["quantile"] for _, labels, _ in samples}
    assert quantiles == {"0.5", "0.95", "0.99"}


def test_quantile_samples_carry_the_monitor_and_type_labels():
    text = render_metrics([(_monitor(name="api-gateway"), _stats(10.0))])

    _, labels, _ = _samples(text, "alerts_monitor_latency_quantile_ms")[0]
    assert labels["monitor"] == "api-gateway"
    assert labels["type"] == "http"


def test_a_monitor_with_no_latency_exports_no_quantile_samples():
    """An absent percentile is left out, not exported as 0."""
    text = render_metrics([(_monitor(), _NO_STATS)])

    assert _samples(text, "alerts_monitor_latency_quantile_ms") == []


def test_the_quantile_family_still_declares_help_and_type():
    text = render_metrics([(_monitor(), _stats(10.0))])

    assert "# HELP alerts_monitor_latency_quantile_ms" in text
    assert "# TYPE alerts_monitor_latency_quantile_ms gauge" in text


def test_a_paused_monitor_exports_no_quantiles():
    text = render_metrics([(_monitor(enabled=False), _stats(10.0))])

    assert _samples(text, "alerts_monitor_latency_quantile_ms") == []


def test_the_metrics_route_serves_the_quantiles(db):
    with db.session() as session:
        monitor = _persisted_monitor(session)
        _seed_checks(session, monitor.id, [10.0, 20.0, 30.0, 40.0])

    status, _, body = _call(DashboardApp(db), "/metrics")

    assert status == "200 OK"
    assert b'alerts_monitor_latency_quantile_ms{monitor="site",type="http",quantile="0.95"} 40' in body
