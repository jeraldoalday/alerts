"""State summary tiles on the index and the /api/summary endpoint."""
import json

from alerts.models import Incident, IncidentState, Monitor, MonitorState, MonitorType
from alerts.web import render
from alerts.web.app import DashboardApp
from alerts.web.queries import count_open_incidents
from alerts.web.stats import StateSummary, summarize_states


def _monitor(name: str, state: MonitorState) -> Monitor:
    return Monitor(
        name=name, type=MonitorType.HTTP, target="https://example.com", state=state
    )


def _get(app, path: str):
    status = []
    body = b"".join(
        app({"REQUEST_METHOD": "GET", "PATH_INFO": path}, lambda s, h: status.append(s))
    )
    return status[0], body.decode("utf-8")


def test_summarize_counts_every_state_including_the_empty_ones():
    summary = summarize_states(
        [
            _monitor("a", MonitorState.UP),
            _monitor("b", MonitorState.UP),
            _monitor("c", MonitorState.DOWN),
        ]
    )

    assert summary.total == 3
    assert summary.counts[MonitorState.UP] == 2
    assert summary.counts[MonitorState.DOWN] == 1
    assert summary.counts[MonitorState.DEGRADED] == 0
    assert summary.counts[MonitorState.UNKNOWN] == 0


def test_summarize_carries_the_open_incident_count():
    assert summarize_states([], open_incidents=4).open_incidents == 4


def test_all_up_is_false_without_any_monitors():
    assert summarize_states([]).all_up is False
    assert summarize_states([_monitor("a", MonitorState.UP)]).all_up is True
    assert summarize_states([_monitor("a", MonitorState.DOWN)]).all_up is False


def test_count_open_incidents_ignores_resolved_ones(db):
    with db.session() as session:
        monitor = _monitor("site", MonitorState.DOWN)
        session.add(monitor)
        session.flush()
        session.add(Incident(monitor_id=monitor.id, state=IncidentState.OPEN))
        session.add(Incident(monitor_id=monitor.id, state=IncidentState.RESOLVED))

    with db.session() as session:
        assert count_open_incidents(session) == 1


def test_summary_renders_a_tile_per_state():
    html = render.render_summary(
        StateSummary(
            total=3,
            counts={
                MonitorState.UP: 1,
                MonitorState.DEGRADED: 1,
                MonitorState.DOWN: 1,
                MonitorState.UNKNOWN: 0,
            },
            open_incidents=1,
        )
    )

    for label in ("monitors", "up", "degraded", "down", "open incidents"):
        assert f">{label}</span>" in html
    assert "unknown" not in html, "a state nothing is in is noise"


def test_summary_keeps_the_down_tile_at_zero():
    """'0 down' is the reassurance an operator opens the dashboard for."""
    html = render.render_summary(
        StateSummary(
            total=1,
            counts={
                MonitorState.UP: 1,
                MonitorState.DEGRADED: 0,
                MonitorState.DOWN: 0,
                MonitorState.UNKNOWN: 0,
            },
        )
    )

    assert ">down</span>" in html
    assert ">degraded</span>" not in html


def test_summary_shows_unknown_only_when_something_is_unknown():
    html = render.render_summary(
        StateSummary(
            total=1,
            counts={
                MonitorState.UP: 0,
                MonitorState.DEGRADED: 0,
                MonitorState.DOWN: 0,
                MonitorState.UNKNOWN: 1,
            },
        )
    )

    assert ">unknown</span>" in html


def test_index_page_includes_the_summary(db):
    with db.session() as session:
        session.add(_monitor("a", MonitorState.UP))
        session.add(_monitor("b", MonitorState.DOWN))

    _, body = _get(DashboardApp(db), "/")

    assert '<ul class="summary">' in body
    assert body.index('class="summary"') < body.index("<table>"), "summary leads the page"


def test_index_renders_without_a_summary_argument():
    """render_index stays usable on its own, summary or not."""
    assert '<ul class="summary">' not in render.render_index([])


def test_api_summary_returns_state_counts(db):
    with db.session() as session:
        up = _monitor("a", MonitorState.UP)
        down = _monitor("b", MonitorState.DOWN)
        session.add_all([up, down])
        session.flush()
        session.add(Incident(monitor_id=down.id, state=IncidentState.OPEN))

    status, body = _get(DashboardApp(db), "/api/summary")

    assert status == "200 OK"
    assert json.loads(body) == {
        "total": 2,
        "open_incidents": 1,
        "states": {"unknown": 0, "up": 1, "degraded": 0, "down": 1},
    }


def test_api_summary_on_an_empty_database(db):
    status, body = _get(DashboardApp(db), "/api/summary")

    assert status == "200 OK"
    assert json.loads(body)["total"] == 0
