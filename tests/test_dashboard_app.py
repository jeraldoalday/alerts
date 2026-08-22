import json

from alerts.models import CheckResult, Monitor, MonitorState, MonitorType
from alerts.web.app import DashboardApp


def _environ(path: str, method: str = "GET") -> dict:
    return {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": ""}


def _call(app: DashboardApp, path: str, method: str = "GET"):
    captured = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = dict(headers)

    body = b"".join(app(_environ(path, method), start_response))
    return captured["status"], captured["headers"], body


def _monitor(session, **overrides) -> Monitor:
    defaults = dict(
        name="site", type=MonitorType.HTTP, target="https://example.com", state=MonitorState.UP
    )
    defaults.update(overrides)
    monitor = Monitor(**defaults)
    session.add(monitor)
    session.flush()
    return monitor


def test_index_returns_200_with_no_monitors(db):
    app = DashboardApp(db)

    status, headers, body = _call(app, "/")

    assert status == "200 OK"
    assert headers["Content-Type"].startswith("text/html")
    assert b"No monitors configured." in body


def test_index_lists_configured_monitors(db):
    with db.session() as session:
        _monitor(session, name="site")

    app = DashboardApp(db)
    status, _, body = _call(app, "/")

    assert status == "200 OK"
    assert b"site" in body


def test_monitor_detail_returns_200_for_known_monitor(db):
    with db.session() as session:
        monitor = _monitor(session)
        session.add(CheckResult(monitor_id=monitor.id, success=True, latency_ms=12.0))

    app = DashboardApp(db)
    status, _, body = _call(app, "/monitors/site")

    assert status == "200 OK"
    assert b"site" in body


def test_monitor_detail_returns_404_for_unknown_monitor(db):
    app = DashboardApp(db)

    status, _, body = _call(app, "/monitors/ghost")

    assert status == "404 Not Found"
    assert b"not found" in body


def test_unknown_route_returns_404(db):
    app = DashboardApp(db)

    status, _, _ = _call(app, "/nope")

    assert status == "404 Not Found"


def test_head_returns_the_get_headers_with_no_body(db):
    """The cheapest uptime check there is, including against this app."""
    app = DashboardApp(db)

    get_status, get_headers, get_body = _call(app, "/healthz")
    head_status, head_headers, head_body = _call(app, "/healthz", method="HEAD")

    assert head_status == get_status == "200 OK"
    assert head_headers == get_headers, "same content type and length as the GET"
    assert head_body == b""
    assert get_body != b""


def test_head_on_a_missing_monitor_still_reports_404(db):
    app = DashboardApp(db)

    status, _, body = _call(app, "/monitors/nope", method="HEAD")

    assert status == "404 Not Found"
    assert body == b""


def test_head_on_the_index_runs_the_route(db):
    """The Content-Length has to describe the page a GET would have returned."""
    with db.session() as session:
        _monitor(session, name="site")
    app = DashboardApp(db)

    _, head_headers, head_body = _call(app, "/", method="HEAD")
    _, get_headers, get_body = _call(app, "/")

    assert head_body == b""
    assert head_headers["Content-Length"] == get_headers["Content-Length"]
    assert b"site" in get_body


def test_non_get_method_returns_405(db):
    app = DashboardApp(db)

    status, _, _ = _call(app, "/", method="POST")

    assert status == "405 Method Not Allowed"


def test_healthz_returns_ok_json(db):
    app = DashboardApp(db)

    status, headers, body = _call(app, "/healthz")

    assert status == "200 OK"
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body) == {"status": "ok"}


def test_api_monitors_returns_json_summary(db):
    with db.session() as session:
        monitor = _monitor(session, name="api-site", state=MonitorState.DOWN)
        session.add(CheckResult(monitor_id=monitor.id, success=False, latency_ms=None))

    app = DashboardApp(db)
    status, headers, body = _call(app, "/api/monitors")

    assert status == "200 OK"
    payload = json.loads(body)
    assert payload["monitors"] == [
        {
            "name": "api-site",
            "type": "http",
            "tags": [],
            "state": "down",
            "paused": False,
            "degraded_latency_ms": None,
            "uptime_percentage": 0.0,
            "average_latency_ms": None,
            "latency_percentiles": {"p50": None, "p95": None, "p99": None},
        }
    ]


def test_api_monitors_returns_empty_list_when_no_monitors(db):
    app = DashboardApp(db)

    _, _, body = _call(app, "/api/monitors")

    assert json.loads(body) == {"monitors": [], "tag": None}


def test_api_monitor_detail_returns_checks_and_incidents(db):
    with db.session() as session:
        monitor = _monitor(session, name="site", state=MonitorState.UP)
        session.add(CheckResult(monitor_id=monitor.id, success=True, latency_ms=15.0, status_code=200))

    app = DashboardApp(db)
    status, headers, body = _call(app, "/api/monitors/site")

    assert status == "200 OK"
    assert headers["Content-Type"] == "application/json"
    payload = json.loads(body)
    assert payload["name"] == "site"
    assert payload["type"] == "http"
    assert payload["state"] == "up"
    assert payload["uptime_percentage"] == 100.0
    assert len(payload["recent_checks"]) == 1
    assert payload["recent_checks"][0]["success"] is True
    assert payload["incidents"] == []


def test_api_monitor_detail_returns_404_json_for_unknown_monitor(db):
    app = DashboardApp(db)

    status, headers, body = _call(app, "/api/monitors/ghost")

    assert status == "404 Not Found"
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body) == {"error": "monitor 'ghost' not found"}


def test_api_monitor_detail_includes_the_latency_budget(db):
    with db.session() as session:
        _monitor(session, degraded_latency_ms=1500.0)

    _, _, body = _call(DashboardApp(db), "/api/monitors/site")

    assert json.loads(body)["degraded_latency_ms"] == 1500.0


def test_api_monitors_reports_a_null_budget_when_unset(db):
    with db.session() as session:
        _monitor(session)

    _, _, body = _call(DashboardApp(db), "/api/monitors")

    assert json.loads(body)["monitors"][0]["degraded_latency_ms"] is None
