"""Dependencies on every surface that reports them.

The point of these is agreement. One broken database has to produce the
same story in `alerts deps`, on the index, on a monitor's own page, in the
JSON, and in the exposition -- so most of what follows checks that the
same database says the same thing in each of the five places rather than
checking each rendering on its own.

The story has two halves that must not be conflated anywhere: `checkout`
is down with a down upstream, so its page is being held back; `api` is up
with the same down upstream, so nothing of its is being suppressed at
all.
"""
import json

from alerts.models import Monitor, MonitorState, MonitorType
from alerts.reporting import collect_dependency_rows
from alerts.web import queries, render
from alerts.web.app import DashboardApp
from alerts.web.metrics import parse_sample, render_metrics
from alerts.web.stats import UptimeStats, summarize_states

_NO_STATS = UptimeStats(
    total_checks=0, successful_checks=0, uptime_percentage=None, average_latency_ms=None
)


def _environ(path: str, query: str = "") -> dict:
    return {"REQUEST_METHOD": "GET", "PATH_INFO": path, "QUERY_STRING": query}


def _call(app: DashboardApp, path: str, query: str = ""):
    captured = {}

    def start_response(status, headers):
        captured["status"] = status

    body = b"".join(app(_environ(path, query), start_response))
    return captured["status"], body


def _monitor(session=None, name="api", state=MonitorState.UP, depends_on=(), **overrides):
    fields = dict(
        name=name,
        type=MonitorType.HTTP,
        target=f"https://{name}.example.com",
        state=state,
        depends_on=list(depends_on),
        tags=[],
        enabled=True,
    )
    fields.update(overrides)
    monitor = Monitor(**fields)
    if session is not None:
        session.add(monitor)
        session.flush()
    return monitor


def _estate(session):
    """A checkout page and an API over one broken database."""
    _monitor(session, "checkout", MonitorState.DOWN, ["api"])
    _monitor(session, "api", MonitorState.UP, ["db"])
    _monitor(session, "db", MonitorState.DOWN)


def _rows(session):
    return [
        (monitor, _NO_STATS, None)
        for monitor in queries.list_monitors(session)
    ]


def _samples(body: str):
    parsed = [parse_sample(line) for line in body.splitlines()]
    return [sample for sample in parsed if sample is not None]


def test_the_query_layer_reports_the_down_upstreams(db):
    with db.session() as session:
        _estate(session)

    with db.session() as session:
        blocked = queries.blocking_upstreams(session)

    assert blocked == {"checkout": ["db"], "api": ["db"]}


def test_the_query_layer_says_nothing_when_no_edges_exist(db):
    with db.session() as session:
        _monitor(session, "solo", MonitorState.DOWN)

    with db.session() as session:
        assert queries.blocking_upstreams(session) == {}


def test_the_report_and_the_query_layer_agree(db):
    with db.session() as session:
        _estate(session)

    with db.session() as session:
        rows = {row.name: row for row in collect_dependency_rows(session)}
        blocked = queries.blocking_upstreams(session)

    assert rows["checkout"].blocked_by == blocked["checkout"]
    assert rows["checkout"].alerts_held_back
    assert not rows["api"].alerts_held_back


def test_the_index_marks_the_monitor_whose_alerts_are_held_back(db):
    with db.session() as session:
        _estate(session)

    with db.session() as session:
        html = render.render_index(
            _rows(session),
            summarize_states(queries.list_monitors(session)),
            blocked=queries.blocking_upstreams(session),
        )

    assert "alerts held back" in html


def test_the_index_does_not_claim_a_healthy_monitor_is_suppressed(db):
    with db.session() as session:
        _monitor(session, "api", MonitorState.UP, ["db"])
        _monitor(session, "db", MonitorState.DOWN)

    with db.session() as session:
        html = render.render_index(
            _rows(session),
            summarize_states(queries.list_monitors(session)),
            blocked=queries.blocking_upstreams(session),
        )

    assert "held back" not in html
    assert "db down" in html


def test_the_index_counts_held_back_alerts_in_a_tile(db):
    with db.session() as session:
        _estate(session)

    with db.session() as session:
        html = render.render_index(
            _rows(session),
            summarize_states(queries.list_monitors(session)),
            blocked=queries.blocking_upstreams(session),
        )

    assert "alerts held back" in html
    assert '<span class="count" style="color:#cf222e">1</span>' in html


def test_an_estate_with_no_dependencies_gets_no_tile(db):
    with db.session() as session:
        _monitor(session, "solo", MonitorState.DOWN)

    with db.session() as session:
        html = render.render_index(
            _rows(session), summarize_states(queries.list_monitors(session))
        )

    assert "held back" not in html


def test_the_monitor_page_names_both_directions(db):
    with db.session() as session:
        _estate(session)

    app = DashboardApp(db)
    _, body = _call(app, "/monitors/api")
    html = body.decode()

    assert "Dependencies" in html
    assert 'href="/monitors/db"' in html, "what it needs"
    assert 'href="/monitors/checkout"' in html, "what needs it"


def test_the_monitor_page_of_something_outside_the_graph_has_no_section(db):
    with db.session() as session:
        _monitor(session, "solo")

    _, body = _call(DashboardApp(db), "/monitors/solo")

    assert "Dependencies" not in body.decode()


def test_the_monitor_page_explains_a_held_back_alert(db):
    with db.session() as session:
        _estate(session)

    _, body = _call(DashboardApp(db), "/monitors/checkout")

    assert "alerts held back" in body.decode()


def test_the_json_listing_carries_both_facts(db):
    with db.session() as session:
        _estate(session)

    _, body = _call(DashboardApp(db), "/api/monitors")
    monitors = {row["name"]: row for row in json.loads(body)["monitors"]}

    assert monitors["checkout"]["depends_on"] == ["api"]
    assert monitors["checkout"]["blocked_by"] == ["db"]
    assert monitors["checkout"]["alerts_held_back"] is True
    assert monitors["api"]["blocked_by"] == ["db"]
    assert monitors["api"]["alerts_held_back"] is False


def test_the_json_detail_carries_what_needs_this_monitor(db):
    with db.session() as session:
        _estate(session)

    _, body = _call(DashboardApp(db), "/api/monitors/api")
    payload = json.loads(body)

    assert payload["depends_on"] == ["db"]
    assert payload["needed_by"] == ["checkout"]
    assert payload["blocked_by"] == ["db"]
    assert payload["alerts_held_back"] is False


def test_the_summary_counts_both_figures(db):
    with db.session() as session:
        _estate(session)

    _, body = _call(DashboardApp(db), "/api/summary")

    assert json.loads(body)["dependencies"] == {
        "with_upstream_down": 2,
        "alerts_held_back": 1,
    }


def test_the_summary_reports_zero_on_an_estate_with_no_edges(db):
    with db.session() as session:
        _monitor(session, "solo", MonitorState.DOWN)

    _, body = _call(DashboardApp(db), "/api/summary")

    assert json.loads(body)["dependencies"] == {
        "with_upstream_down": 0,
        "alerts_held_back": 0,
    }


def test_the_exposition_carries_one_series_per_edge(db):
    with db.session() as session:
        _estate(session)

    with db.session() as session:
        monitors = queries.list_monitors(session)
        body = render_metrics(
            [(monitor, _NO_STATS) for monitor in monitors],
            blocked=queries.blocking_upstreams(session, monitors),
        )

    edges = {
        (labels["monitor"], labels["upstream"])
        for name, labels, _ in _samples(body)
        if name == "alerts_monitor_depends_on"
    }
    assert edges == {("checkout", "api"), ("api", "db")}


def test_the_exposition_reports_the_suppression_for_every_monitor(db):
    with db.session() as session:
        _estate(session)

    with db.session() as session:
        monitors = queries.list_monitors(session)
        body = render_metrics(
            [(monitor, _NO_STATS) for monitor in monitors],
            blocked=queries.blocking_upstreams(session, monitors),
        )

    held_back = {
        labels["monitor"]: value
        for name, labels, value in _samples(body)
        if name == "alerts_monitor_alerts_held_back"
    }
    # Every monitor, zeroes included: a scraper cannot tell an absent
    # series from a reading of zero, and zero is what an alert rule on this
    # needs to see when things go back to normal.
    assert held_back == {"checkout": "1", "api": "0", "db": "0"}


def test_the_exposition_counts_the_down_upstreams(db):
    with db.session() as session:
        _estate(session)

    with db.session() as session:
        monitors = queries.list_monitors(session)
        body = render_metrics(
            [(monitor, _NO_STATS) for monitor in monitors],
            blocked=queries.blocking_upstreams(session, monitors),
        )

    counts = {
        labels["monitor"]: value
        for name, labels, value in _samples(body)
        if name == "alerts_monitor_upstream_down"
    }
    assert counts == {"checkout": "1", "api": "1", "db": "0"}


def test_the_exposition_skips_a_paused_monitor_entirely(db):
    with db.session() as session:
        _monitor(session, "checkout", MonitorState.DOWN, ["db"], enabled=False)
        _monitor(session, "db", MonitorState.DOWN)

    with db.session() as session:
        monitors = queries.list_monitors(session)
        body = render_metrics(
            [(monitor, _NO_STATS) for monitor in monitors],
            blocked=queries.blocking_upstreams(session, monitors),
        )

    assert "checkout" not in body


def test_every_surface_agrees_about_who_is_being_held_back(db):
    with db.session() as session:
        _estate(session)

    with db.session() as session:
        report = {
            row.name for row in collect_dependency_rows(session) if row.alerts_held_back
        }
        monitors = queries.list_monitors(session)
        exposition = {
            labels["monitor"]
            for name, labels, value in _samples(
                render_metrics(
                    [(monitor, _NO_STATS) for monitor in monitors],
                    blocked=queries.blocking_upstreams(session, monitors),
                )
            )
            if name == "alerts_monitor_alerts_held_back" and value == "1"
        }

    _, body = _call(DashboardApp(db), "/api/monitors")
    api = {row["name"] for row in json.loads(body)["monitors"] if row["alerts_held_back"]}

    assert report == exposition == api == {"checkout"}
