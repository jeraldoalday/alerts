"""Request method and headers on HTTP monitors.

Covers the whole path: config validation, what `sync_monitors` writes to the
row, what the checker actually puts on the wire, and what the scheduler
records when a header's environment variable has gone missing.
"""
import asyncio

import httpx
import pytest
import yaml

from alerts.checkers.base import CheckRequest
from alerts.checkers.http import HttpChecker
from alerts.config import ConfigError, MonitorConfig, load_config
from alerts.envsubst import MissingEnvVar
from alerts.models import CheckResult, Monitor, MonitorState, MonitorType
from alerts.scheduler import Scheduler, sync_monitors


def _checker(handler) -> HttpChecker:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    return HttpChecker(client=client)


def _request(**overrides) -> CheckRequest:
    defaults = dict(target="https://example.com", timeout_seconds=5)
    defaults.update(overrides)
    return CheckRequest(**defaults)


def _monitor(**overrides) -> Monitor:
    defaults = dict(
        name="site",
        type=MonitorType.HTTP,
        target="https://example.com",
        timeout_seconds=5,
        expected_status=200,
        state=MonitorState.UP,
        method="GET",
        headers={},
        interval_seconds=3600,
        enabled=True,
    )
    defaults.update(overrides)
    return Monitor(**defaults)


def _write_and_load(tmp_path, raw: dict):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.safe_dump(raw))
    return load_config(config_file)


# --- config ----------------------------------------------------------------


def test_the_method_defaults_to_get():
    assert MonitorConfig(name="site", type="http", target="https://example.com").method == "GET"


def test_a_lowercase_method_is_normalised():
    """YAML is written by hand; "head" and "HEAD" mean the same request."""
    monitor = MonitorConfig(
        name="site", type="http", target="https://example.com", method="head"
    )

    assert monitor.method == "HEAD"


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_the_supported_methods_are_accepted(method):
    monitor = MonitorConfig(
        name="site", type="http", target="https://example.com", method=method
    )

    assert monitor.method == method


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "TRACE", "nonsense"])
def test_a_method_with_no_body_to_send_is_rejected(method):
    """There is nowhere in a monitor's config to put a request body."""
    with pytest.raises(ValueError, match="unsupported HTTP method"):
        MonitorConfig(name="site", type="http", target="https://example.com", method=method)


def test_headers_default_to_empty():
    assert MonitorConfig(name="site", type="http", target="https://example.com").headers == {}


def test_headers_are_accepted_on_an_http_monitor():
    monitor = MonitorConfig(
        name="site",
        type="http",
        target="https://example.com",
        headers={"Accept": "application/json"},
    )

    assert monitor.headers == {"Accept": "application/json"}


def test_a_blank_header_name_is_rejected():
    with pytest.raises(ValueError, match="header names must not be blank"):
        MonitorConfig(
            name="site", type="http", target="https://example.com", headers={"  ": "v"}
        )


@pytest.mark.parametrize(
    "monitor_type,target",
    [("tcp", "db.internal:5432"), ("dns", "example.com"), ("ssl", "example.com:443")],
)
def test_headers_on_a_monitor_that_sends_no_request_are_rejected(monitor_type, target):
    """Otherwise an Authorization header implies a check that isn't happening."""
    with pytest.raises(ValueError, match="issues no HTTP request"):
        MonitorConfig(
            name="thing", type=monitor_type, target=target, headers={"Authorization": "x"}
        )


def test_a_method_on_a_monitor_that_sends_no_request_is_rejected():
    with pytest.raises(ValueError, match="issues no HTTP request"):
        MonitorConfig(name="thing", type="tcp", target="db.internal:5432", method="HEAD")


def test_the_default_method_on_a_non_http_monitor_is_fine():
    """Only an explicitly wrong option is an error, not the untouched default."""
    monitor = MonitorConfig(name="thing", type="tcp", target="db.internal:5432")

    assert monitor.method == "GET"


def test_a_body_assertion_alongside_head_is_rejected():
    """HEAD returns no body, so the assertion could never match."""
    with pytest.raises(ValueError, match="HEAD, which returns no body"):
        MonitorConfig(
            name="site",
            type="http",
            target="https://example.com",
            method="HEAD",
            expected_body_contains="ok",
        )


def test_a_body_assertion_alongside_get_is_fine():
    monitor = MonitorConfig(
        name="site",
        type="http",
        target="https://example.com",
        method="GET",
        expected_body_contains="ok",
    )

    assert monitor.expected_body_contains == "ok"


def test_load_config_accepts_a_resolvable_header_placeholder(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "s3cret")

    config = _write_and_load(
        tmp_path,
        {
            "monitors": [
                {
                    "name": "api",
                    "type": "http",
                    "target": "https://example.com/health",
                    "headers": {"Authorization": "Bearer ${API_TOKEN}"},
                }
            ]
        },
    )

    assert config.monitors[0].headers == {"Authorization": "Bearer ${API_TOKEN}"}, (
        "stored as written; expansion happens per check"
    )


def test_load_config_rejects_a_header_placeholder_that_cannot_resolve(tmp_path, monkeypatch):
    monkeypatch.delenv("MISSING_TOKEN", raising=False)

    with pytest.raises(ConfigError, match="MISSING_TOKEN"):
        _write_and_load(
            tmp_path,
            {
                "monitors": [
                    {
                        "name": "api",
                        "type": "http",
                        "target": "https://example.com/health",
                        "headers": {"Authorization": "Bearer ${MISSING_TOKEN}"},
                    }
                ]
            },
        )


# --- persistence -----------------------------------------------------------


def test_sync_monitors_writes_the_method_and_headers(db):
    config = MonitorConfig(
        name="api",
        type="http",
        target="https://example.com",
        method="HEAD",
        headers={"Accept": "application/json"},
    )

    with db.session() as session:
        sync_monitors(session, [config])

    with db.session() as session:
        monitor = session.query(Monitor).one()
        assert monitor.method == "HEAD"
        assert monitor.headers == {"Accept": "application/json"}


def test_sync_monitors_updates_them_on_an_existing_row(db):
    with db.session() as session:
        sync_monitors(session, [MonitorConfig(name="api", type="http", target="https://a")])
    with db.session() as session:
        sync_monitors(
            session,
            [
                MonitorConfig(
                    name="api",
                    type="http",
                    target="https://a",
                    method="OPTIONS",
                    headers={"X-Env": "prod"},
                )
            ],
        )

    with db.session() as session:
        monitor = session.query(Monitor).one()
        assert monitor.method == "OPTIONS"
        assert monitor.headers == {"X-Env": "prod"}


def test_dropping_a_header_from_the_config_removes_it_from_the_row(db):
    with db.session() as session:
        sync_monitors(
            session,
            [
                MonitorConfig(
                    name="api", type="http", target="https://a", headers={"X-Old": "1"}
                )
            ],
        )
    with db.session() as session:
        sync_monitors(session, [MonitorConfig(name="api", type="http", target="https://a")])

    with db.session() as session:
        assert session.query(Monitor).one().headers == {}


def test_check_request_from_monitor_expands_header_placeholders(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "s3cret")
    monitor = _monitor(headers={"Authorization": "Bearer ${API_TOKEN}"}, method="HEAD")

    request = CheckRequest.from_monitor(monitor)

    assert request.headers == {"Authorization": "Bearer s3cret"}
    assert request.method == "HEAD"


def test_check_request_from_monitor_raises_on_a_missing_variable(monkeypatch):
    monkeypatch.delenv("GONE_TOKEN", raising=False)
    monitor = _monitor(headers={"Authorization": "Bearer ${GONE_TOKEN}"})

    with pytest.raises(MissingEnvVar):
        CheckRequest.from_monitor(monitor)


def test_check_request_from_monitor_tolerates_headers_left_null(monkeypatch):
    """A row written before the column existed reads back as NULL, not {}."""
    monitor = _monitor(headers=None)

    assert CheckRequest.from_monitor(monitor).headers == {}


# --- the wire --------------------------------------------------------------


async def test_the_configured_method_is_used():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        return httpx.Response(200)

    outcome = await _checker(handler).check(_request(method="HEAD"))

    assert seen["method"] == "HEAD"
    assert outcome.success is True


async def test_the_default_method_is_get():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        return httpx.Response(200)

    await _checker(handler).check(_request())

    assert seen["method"] == "GET"


async def test_the_configured_headers_are_sent():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200)

    await _checker(handler).check(_request(headers={"Authorization": "Bearer s3cret"}))

    assert seen["auth"] == "Bearer s3cret"


async def test_sending_no_headers_still_works():
    outcome = await _checker(lambda request: httpx.Response(200)).check(_request())

    assert outcome.success is True


async def test_a_head_response_is_judged_on_its_status_alone():
    checker = _checker(lambda request: httpx.Response(204))

    outcome = await checker.check(_request(method="HEAD", expected_status=204))

    assert outcome.success is True
    assert outcome.latency_ms is not None


# --- the scheduler ---------------------------------------------------------


async def test_a_missing_header_variable_is_recorded_as_a_failed_check(db, monkeypatch):
    """Named in the error, so the dashboard shows the fix rather than a gap."""
    monkeypatch.delenv("GONE_TOKEN", raising=False)
    with db.session() as session:
        session.add(
            _monitor(headers={"Authorization": "Bearer ${GONE_TOKEN}"}, failure_threshold=1)
        )

    scheduler = Scheduler(db, checkers={MonitorType.HTTP: _checker(lambda r: httpx.Response(200))})
    run_task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0.05)
    await scheduler.stop()
    await run_task

    with db.session() as session:
        result = session.query(CheckResult).one()
        assert result.success is False
        assert "GONE_TOKEN" in result.error_message
        assert session.query(Monitor).one().state == MonitorState.DOWN
