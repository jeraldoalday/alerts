"""Prometheus exposition rendering and the /metrics route."""
from alerts.models import CheckResult, Incident, IncidentState, Monitor, MonitorState, MonitorType
from alerts.web.app import DashboardApp
from alerts.web.metrics import (
    CONTENT_TYPE,
    escape_label_value,
    parse_sample,
    render_metrics,
    state_values,
)
from alerts.web.stats import UptimeStats

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


def _stats(**overrides) -> UptimeStats:
    defaults = dict(
        total_checks=50,
        successful_checks=49,
        uptime_percentage=98.0,
        average_latency_ms=120.5,
    )
    defaults.update(overrides)
    return UptimeStats(**defaults)


def _samples(text: str, name: str):
    parsed = [parse_sample(line) for line in text.splitlines()]
    return [p for p in parsed if p is not None and p[0] == name]


def _call(app, path: str):
    captured = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = dict(headers)

    body = b"".join(app({"REQUEST_METHOD": "GET", "PATH_INFO": path}, start_response))
    return captured["status"], captured["headers"], body.decode("utf-8")


# --- format ----------------------------------------------------------------


def test_every_family_declares_help_and_type():
    text = render_metrics([(_monitor(), _stats())])

    for family in (
        "alerts_monitors",
        "alerts_open_incidents",
        "alerts_monitor_state",
        "alerts_monitor_uptime_ratio",
        "alerts_monitor_latency_ms",
        "alerts_monitor_checks",
    ):
        assert f"# HELP {family} " in text
        assert f"# TYPE {family} gauge" in text


def test_the_body_ends_with_a_newline():
    """Scrapers reject a final sample line without one."""
    assert render_metrics([(_monitor(), _stats())]).endswith("\n")


def test_help_and_type_precede_the_samples_of_their_family():
    lines = render_metrics([(_monitor(), _stats())]).splitlines()

    help_index = lines.index(
        "# HELP alerts_monitor_uptime_ratio Fraction of recent checks that succeeded, between 0 and 1."
    )
    sample_index = next(
        i for i, line in enumerate(lines) if line.startswith("alerts_monitor_uptime_ratio{")
    )
    assert help_index < sample_index


def test_integers_render_without_a_trailing_zero():
    text = render_metrics([(_monitor(), _stats(total_checks=50))])

    assert _samples(text, "alerts_monitor_checks")[0][2] == "50"


def test_floats_keep_their_precision():
    text = render_metrics([(_monitor(), _stats(average_latency_ms=120.5))])

    assert _samples(text, "alerts_monitor_latency_ms")[0][2] == "120.5"


# --- values ----------------------------------------------------------------


def test_the_state_gauge_is_one_for_the_current_state_and_zero_elsewhere():
    text = render_metrics([(_monitor(state=MonitorState.DOWN), _stats())])

    by_state = {labels["state"]: value for _, labels, value in _samples(text, "alerts_monitor_state")}
    assert by_state == {"unknown": "0", "up": "0", "degraded": "0", "down": "1"}


def test_state_values_matches_what_is_exported():
    assert state_values(_monitor(state=MonitorState.DEGRADED)) == {
        "unknown": 0,
        "up": 0,
        "degraded": 1,
        "down": 0,
    }


def test_uptime_is_exported_as_a_ratio_not_a_percentage():
    text = render_metrics([(_monitor(), _stats(uptime_percentage=98.0))])

    assert _samples(text, "alerts_monitor_uptime_ratio")[0][2] == "0.98"


def test_samples_carry_the_monitor_and_type_labels():
    text = render_metrics([(_monitor("api", type=MonitorType.TCP), _stats())])

    _, labels, _ = _samples(text, "alerts_monitor_checks")[0]
    assert labels["monitor"] == "api"
    assert labels["type"] == "tcp"


def test_a_monitor_with_no_checks_omits_uptime_and_latency():
    """Nothing is a truthful reading of an average over no samples."""
    text = render_metrics([(_monitor(), _NO_STATS)])

    assert _samples(text, "alerts_monitor_uptime_ratio") == []
    assert _samples(text, "alerts_monitor_latency_ms") == []
    assert _samples(text, "alerts_monitor_checks")[0][2] == "0"


def test_a_latency_budget_is_exported_when_set():
    text = render_metrics([(_monitor(degraded_latency_ms=1500.0), _stats())])

    assert _samples(text, "alerts_monitor_latency_budget_ms")[0][2] == "1500"


def test_no_budget_sample_when_the_monitor_has_none():
    text = render_metrics([(_monitor(), _stats())])

    assert _samples(text, "alerts_monitor_latency_budget_ms") == []


def test_the_monitor_count_and_open_incidents_are_exported():
    text = render_metrics(
        [(_monitor("a"), _stats()), (_monitor("b"), _stats())], open_incidents=3
    )

    assert _samples(text, "alerts_monitors")[0][2] == "2"
    assert _samples(text, "alerts_open_incidents")[0][2] == "3"


def test_paused_monitors_are_not_exported():
    """A scraper can't tell a frozen reading from a live one."""
    text = render_metrics(
        [(_monitor("live"), _stats()), (_monitor("dropped", enabled=False), _stats())]
    )

    exported = {labels["monitor"] for _, labels, _ in _samples(text, "alerts_monitor_checks")}
    assert exported == {"live"}
    assert _samples(text, "alerts_monitors")[0][2] == "1"


def test_an_empty_database_still_renders_the_families():
    text = render_metrics([])

    assert "# TYPE alerts_monitors gauge" in text
    assert _samples(text, "alerts_monitors")[0][2] == "0"


# --- escaping --------------------------------------------------------------


def test_a_quote_in_a_label_is_escaped():
    assert escape_label_value('we"ird') == 'we\\"ird'


def test_a_backslash_is_escaped_before_the_characters_it_would_double():
    assert escape_label_value("back\\slash") == "back\\\\slash"
    assert escape_label_value('\\"') == '\\\\\\"'


def test_a_newline_is_escaped():
    assert escape_label_value("two\nlines") == "two\\nlines"


def test_a_monitor_name_with_a_quote_does_not_break_the_line():
    text = render_metrics([(_monitor('we"ird'), _stats())])

    line = next(line for line in text.splitlines() if line.startswith("alerts_monitor_checks{"))
    assert 'monitor="we\\"ird"' in line


# --- route -----------------------------------------------------------------


def test_metrics_route_serves_the_exposition(db):
    with db.session() as session:
        monitor = _monitor("api-site", state=MonitorState.DOWN)
        session.add(monitor)
        session.flush()
        session.add(CheckResult(monitor_id=monitor.id, success=False, latency_ms=None))
        session.add(Incident(monitor_id=monitor.id, state=IncidentState.OPEN))

    status, headers, body = _call(DashboardApp(db), "/metrics")

    assert status == "200 OK"
    assert headers["Content-Type"] == CONTENT_TYPE
    assert 'alerts_monitor_state{monitor="api-site",type="http",state="down"} 1' in body
    assert "alerts_open_incidents 1" in body


def test_metrics_route_on_an_empty_database(db):
    status, _, body = _call(DashboardApp(db), "/metrics")

    assert status == "200 OK"
    assert "alerts_monitors 0" in body


def test_metrics_route_rejects_a_post(db):
    status, _, _ = _call_post(DashboardApp(db))

    assert status == "405 Method Not Allowed"


def _call_post(app):
    captured = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = dict(headers)

    b"".join(app({"REQUEST_METHOD": "POST", "PATH_INFO": "/metrics"}, start_response))
    return captured["status"], captured["headers"], ""


def test_metrics_content_length_matches_the_body(db):
    with db.session() as session:
        session.add(_monitor("site"))

    _, headers, body = _call(DashboardApp(db), "/metrics")

    assert int(headers["Content-Length"]) == len(body.encode("utf-8"))


def test_parse_sample_ignores_comments_and_blank_lines():
    assert parse_sample("# HELP x y") is None
    assert parse_sample("") is None
