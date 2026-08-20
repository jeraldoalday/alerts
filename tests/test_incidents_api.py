"""The /api/incidents feed: filters, parameter validation, and payload shape."""
import json
from datetime import datetime, timedelta

from alerts.models import Incident, IncidentState, Monitor, MonitorState, MonitorType
from alerts.timeutil import utcnow
from alerts.web.app import DashboardApp


def _call(app: DashboardApp, path: str, query: str = "", method: str = "GET"):
    captured = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = dict(headers)

    body = b"".join(
        app(
            {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query},
            start_response,
        )
    )
    return captured["status"], captured["headers"], body


def _get(app: DashboardApp, query: str = ""):
    status, _, body = _call(app, "/api/incidents", query)
    return status, json.loads(body or b"{}")


def _monitor(session, name: str = "site") -> Monitor:
    monitor = Monitor(
        name=name, type=MonitorType.HTTP, target="https://example.com", state=MonitorState.UP
    )
    session.add(monitor)
    session.flush()
    return monitor


def _incident(session, monitor_id: int, *, started_ago, lasted=None, cause="boom") -> None:
    started_at = _naive_now() - started_ago
    session.add(
        Incident(
            monitor_id=monitor_id,
            state=IncidentState.RESOLVED if lasted is not None else IncidentState.OPEN,
            started_at=started_at,
            resolved_at=started_at + lasted if lasted is not None else None,
            cause=cause,
        )
    )


def _naive_now() -> datetime:
    return utcnow().replace(tzinfo=None)


def test_an_empty_database_returns_an_empty_feed(db):
    status, payload = _get(DashboardApp(db))

    assert status == "200 OK"
    assert payload == {"incidents": [], "open": 0}


def test_an_incident_carries_the_monitor_name_state_and_cause(db):
    with db.session() as session:
        monitor = _monitor(session, "api-gateway")
        _incident(
            session,
            monitor.id,
            started_ago=timedelta(hours=1),
            lasted=timedelta(minutes=30),
            cause="expected status 200, got 502",
        )

    _, payload = _get(DashboardApp(db))

    incident = payload["incidents"][0]
    assert incident["monitor"] == "api-gateway"
    assert incident["state"] == "resolved"
    assert incident["cause"] == "expected status 200, got 502"
    assert incident["resolved_at"] is not None


def test_a_resolved_incident_reports_the_span_it_covered(db):
    with db.session() as session:
        monitor = _monitor(session)
        _incident(session, monitor.id, started_ago=timedelta(hours=3), lasted=timedelta(hours=1))

    _, payload = _get(DashboardApp(db))

    assert payload["incidents"][0]["duration_seconds"] == 3600


def test_an_open_incident_is_measured_against_now(db):
    with db.session() as session:
        monitor = _monitor(session)
        _incident(session, monitor.id, started_ago=timedelta(minutes=10))

    _, payload = _get(DashboardApp(db))

    incident = payload["incidents"][0]
    assert incident["resolved_at"] is None
    assert 590 <= incident["duration_seconds"] <= 610


def test_the_open_count_tallies_the_listed_incidents(db):
    with db.session() as session:
        monitor = _monitor(session)
        _incident(session, monitor.id, started_ago=timedelta(hours=2), lasted=timedelta(minutes=1))
        _incident(session, monitor.id, started_ago=timedelta(minutes=5))

    _, payload = _get(DashboardApp(db))

    assert len(payload["incidents"]) == 2
    assert payload["open"] == 1


def test_incidents_are_newest_first(db):
    with db.session() as session:
        monitor = _monitor(session)
        _incident(session, monitor.id, started_ago=timedelta(days=2), lasted=timedelta(minutes=1))
        _incident(session, monitor.id, started_ago=timedelta(hours=1), lasted=timedelta(minutes=1))

    _, payload = _get(DashboardApp(db))

    starts = [incident["started_at"] for incident in payload["incidents"]]
    assert starts == sorted(starts, reverse=True)


def test_the_monitor_filter_narrows_the_feed(db):
    with db.session() as session:
        first = _monitor(session, "api")
        second = _monitor(session, "site")
        _incident(session, first.id, started_ago=timedelta(hours=1), lasted=timedelta(minutes=1))
        _incident(session, second.id, started_ago=timedelta(hours=2), lasted=timedelta(minutes=1))

    _, payload = _get(DashboardApp(db), "monitor=api")

    assert [i["monitor"] for i in payload["incidents"]] == ["api"]


def test_an_unknown_monitor_is_a_404(db):
    status, payload = _get(DashboardApp(db), "monitor=nope")

    assert status == "404 Not Found"
    assert "not found" in payload["error"]


def test_the_open_flag_drops_resolved_incidents(db):
    with db.session() as session:
        monitor = _monitor(session)
        _incident(session, monitor.id, started_ago=timedelta(hours=2), lasted=timedelta(minutes=1))
        _incident(session, monitor.id, started_ago=timedelta(minutes=5))

    _, payload = _get(DashboardApp(db), "open")

    assert payload["open"] == len(payload["incidents"]) == 1


def test_the_open_flag_accepts_the_usual_spellings(db):
    app = DashboardApp(db)

    for spelling in ("open", "open=1", "open=true", "open=YES", "open=on"):
        _, payload = _get(app, spelling)
        assert payload == {"incidents": [], "open": 0}, spelling


def test_open_false_leaves_the_feed_unfiltered(db):
    with db.session() as session:
        monitor = _monitor(session)
        _incident(session, monitor.id, started_ago=timedelta(hours=1), lasted=timedelta(minutes=1))

    _, payload = _get(DashboardApp(db), "open=false")

    assert len(payload["incidents"]) == 1


def test_the_days_window_excludes_older_incidents(db):
    with db.session() as session:
        monitor = _monitor(session)
        _incident(session, monitor.id, started_ago=timedelta(days=30), lasted=timedelta(minutes=1))

    _, payload = _get(DashboardApp(db), "days=7")

    assert payload["incidents"] == []


def test_days_zero_reports_everything_kept(db):
    with db.session() as session:
        monitor = _monitor(session)
        _incident(session, monitor.id, started_ago=timedelta(days=30), lasted=timedelta(minutes=1))

    _, payload = _get(DashboardApp(db), "days=0")

    assert len(payload["incidents"]) == 1


def test_an_open_incident_outlives_the_window(db):
    """The one row a report must never lose: still broken, started long ago."""
    with db.session() as session:
        monitor = _monitor(session)
        _incident(session, monitor.id, started_ago=timedelta(days=30))

    _, payload = _get(DashboardApp(db), "days=7")

    assert payload["open"] == 1


def test_the_limit_caps_the_feed(db):
    with db.session() as session:
        monitor = _monitor(session)
        for hours in range(1, 6):
            _incident(
                session, monitor.id, started_ago=timedelta(hours=hours), lasted=timedelta(minutes=1)
            )

    _, payload = _get(DashboardApp(db), "limit=2")

    assert len(payload["incidents"]) == 2


def test_a_non_numeric_limit_is_a_400(db):
    status, payload = _get(DashboardApp(db), "limit=lots")

    assert status == "400 Bad Request"
    assert payload["error"] == "limit must be an integer"


def test_an_out_of_range_limit_is_refused_rather_than_clamped(db):
    """Silently returning fewer rows reads as "that's all there was"."""
    status, payload = _get(DashboardApp(db), "limit=5000")

    assert status == "400 Bad Request"
    assert "between 1 and 500" in payload["error"]


def test_a_zero_limit_is_refused(db):
    status, _ = _get(DashboardApp(db), "limit=0")

    assert status == "400 Bad Request"


def test_a_negative_days_window_is_refused(db):
    status, _ = _get(DashboardApp(db), "days=-1")

    assert status == "400 Bad Request"


def test_a_400_still_carries_a_json_content_type(db):
    _, headers, _ = _call(DashboardApp(db), "/api/incidents", "limit=x")

    assert headers["Content-Type"] == "application/json"


def test_head_on_the_feed_returns_headers_only(db):
    status, headers, body = _call(DashboardApp(db), "/api/incidents", method="HEAD")

    assert status == "200 OK"
    assert body == b""
    assert int(headers["Content-Length"]) > 0


def test_query_parameters_do_not_leak_into_other_routes(db):
    """Only this route reads them; /healthz answering 400 would be absurd."""
    status, _, _ = _call(DashboardApp(db), "/healthz", "limit=nonsense")

    assert status == "200 OK"
