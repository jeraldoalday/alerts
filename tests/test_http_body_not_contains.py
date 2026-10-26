"""The expected_body_not_contains assertion on HTTP monitors.

The mirror image of expected_body_contains: a check that answers 200 while
serving a maintenance banner or a degraded-mode notice still looks healthy
to a status-only check, and still contains the substring an
expected_body_contains assertion is looking for the absence of, not the
presence. These cover the negative-match assertion from config validation
through to what the scheduler persists.
"""
import httpx
import pytest

from alerts.checkers.base import CheckRequest
from alerts.checkers.http import HttpChecker
from alerts.config import ConfigError, MonitorConfig, load_config
from alerts.models import Monitor
from alerts.scheduler import sync_monitors


def _checker(handler) -> HttpChecker:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    return HttpChecker(client=client)


def _request(**overrides) -> CheckRequest:
    defaults = dict(target="https://example.com", timeout_seconds=5)
    defaults.update(overrides)
    return CheckRequest(**defaults)


async def test_body_missing_the_forbidden_substring_passes():
    checker = _checker(lambda request: httpx.Response(200, text="status: ok"))

    outcome = await checker.check(_request(expected_body_not_contains="maintenance"))

    assert outcome.success is True
    assert outcome.error_message is None


async def test_body_containing_the_forbidden_substring_fails():
    checker = _checker(lambda request: httpx.Response(200, text="under maintenance, back soon"))

    outcome = await checker.check(_request(expected_body_not_contains="maintenance"))

    assert outcome.success is False
    assert "contains" in outcome.error_message
    assert "maintenance" in outcome.error_message
    assert outcome.status_code == 200, "the response still gets reported"
    assert outcome.latency_ms is not None


async def test_no_assertion_configured_ignores_the_body():
    checker = _checker(lambda request: httpx.Response(200, text="under maintenance"))

    outcome = await checker.check(_request())

    assert outcome.success is True


async def test_the_match_is_case_sensitive():
    checker = _checker(lambda request: httpx.Response(200, text="MAINTENANCE"))

    outcome = await checker.check(_request(expected_body_not_contains="maintenance"))

    assert outcome.success is True


async def test_a_wrong_status_is_reported_ahead_of_the_body():
    """The status is the more useful diagnosis when both assertions fail."""
    checker = _checker(lambda request: httpx.Response(500, text="under maintenance"))

    outcome = await checker.check(_request(expected_body_not_contains="maintenance"))

    assert outcome.success is False
    assert "expected status 200, got 500" in outcome.error_message


async def test_a_clean_body_does_not_rescue_a_wrong_status():
    checker = _checker(lambda request: httpx.Response(503, text="status: ok"))

    outcome = await checker.check(_request(expected_body_not_contains="maintenance"))

    assert outcome.success is False


async def test_both_assertions_can_be_combined():
    """A positive and negative assertion check independent things, not one
    another's negation -- a body that satisfies both passes."""
    checker = _checker(lambda request: httpx.Response(200, text="status: ok, all clear"))

    outcome = await checker.check(
        _request(expected_body_contains="ok", expected_body_not_contains="maintenance")
    )

    assert outcome.success is True


async def test_the_positive_assertion_is_checked_before_the_negative_one():
    checker = _checker(lambda request: httpx.Response(200, text="under maintenance"))

    outcome = await checker.check(
        _request(expected_body_contains="all clear", expected_body_not_contains="maintenance")
    )

    assert outcome.success is False
    assert "does not contain" in outcome.error_message


def test_config_accepts_the_assertion_on_an_http_monitor():
    config = MonitorConfig(
        name="site",
        type="http",
        target="https://example.com",
        expected_body_not_contains="maintenance",
    )

    assert config.expected_body_not_contains == "maintenance"


@pytest.mark.parametrize("monitor_type,target", [("tcp", "db:5432"), ("dns", "example.com")])
def test_config_rejects_the_assertion_on_a_type_with_no_body(monitor_type, target):
    with pytest.raises(ValueError, match="never reads a response body"):
        MonitorConfig(
            name="thing",
            type=monitor_type,
            target=target,
            expected_body_not_contains="maintenance",
        )


def test_config_rejects_an_empty_assertion():
    """An empty substring is in every body, so it would fail every check."""
    with pytest.raises(ValueError):
        MonitorConfig(
            name="site",
            type="http",
            target="https://example.com",
            expected_body_not_contains="",
        )


def test_config_rejects_the_assertion_on_a_head_request():
    with pytest.raises(ValueError, match="returns no body"):
        MonitorConfig(
            name="site",
            type="http",
            target="https://example.com",
            method="HEAD",
            expected_body_not_contains="maintenance",
        )


def test_load_config_surfaces_the_type_mismatch(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "monitors:\n"
        "  - name: db\n"
        "    type: tcp\n"
        "    target: db.example.com:5432\n"
        "    expected_body_not_contains: maintenance\n"
    )

    with pytest.raises(ConfigError, match="never reads a response body"):
        load_config(config_file)


def test_sync_monitors_persists_the_assertion(db):
    with db.session() as session:
        sync_monitors(
            session,
            [
                MonitorConfig(
                    name="site",
                    type="http",
                    target="https://example.com",
                    expected_body_not_contains="maintenance",
                )
            ],
        )

    with db.session() as session:
        assert session.query(Monitor).one().expected_body_not_contains == "maintenance"


def test_sync_monitors_clears_an_assertion_removed_from_the_config(db):
    with db.session() as session:
        sync_monitors(
            session,
            [
                MonitorConfig(
                    name="site",
                    type="http",
                    target="https://example.com",
                    expected_body_not_contains="maintenance",
                )
            ],
        )

    with db.session() as session:
        sync_monitors(
            session, [MonitorConfig(name="site", type="http", target="https://example.com")]
        )

    with db.session() as session:
        assert session.query(Monitor).one().expected_body_not_contains is None


def test_check_request_carries_the_assertion_from_the_monitor(db):
    with db.session() as session:
        sync_monitors(
            session,
            [
                MonitorConfig(
                    name="site",
                    type="http",
                    target="https://example.com",
                    expected_body_not_contains="maintenance",
                )
            ],
        )

    with db.session() as session:
        request = CheckRequest.from_monitor(session.query(Monitor).one())

    assert request.expected_body_not_contains == "maintenance"
