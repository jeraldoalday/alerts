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
            "state": "down",
            "uptime_percentage": 0.0,
            "average_latency_ms": None,
        }
    ]


def test_api_monitors_returns_empty_list_when_no_monitors(db):
    app = DashboardApp(db)

    _, _, body = _call(app, "/api/monitors")

    assert json.loads(body) == {"monitors": []}
