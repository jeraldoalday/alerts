"""The expected_body_contains assertion on HTTP monitors.

The status code alone is a weak health signal: an endpoint that answers 200
with a body naming a broken dependency passes a status-only check. These
cover the substring assertion layered on top of it, from config validation
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


async def test_body_containing_the_substring_passes():
    checker = _checker(lambda request: httpx.Response(200, text="status: ok"))

    outcome = await checker.check(_request(expected_body_contains="ok"))

    assert outcome.success is True
    assert outcome.error_message is None


async def test_body_missing_the_substring_fails():
    checker = _checker(lambda request: httpx.Response(200, text="database unreachable"))

    outcome = await checker.check(_request(expected_body_contains="all systems go"))

    assert outcome.success is False
    assert "does not contain" in outcome.error_message
    assert "all systems go" in outcome.error_message
    assert outcome.status_code == 200, "the response still gets reported"
    assert outcome.latency_ms is not None


async def test_no_assertion_configured_ignores_the_body():
    checker = _checker(lambda request: httpx.Response(200, text="anything at all"))

    outcome = await checker.check(_request())

    assert outcome.success is True


async def test_the_match_is_a_substring_not_a_whole_body_comparison():
    checker = _checker(lambda request: httpx.Response(200, text='{"status":"healthy","v":3}'))

    outcome = await checker.check(_request(expected_body_contains='"status":"healthy"'))

    assert outcome.success is True


async def test_the_match_is_case_sensitive():
    checker = _checker(lambda request: httpx.Response(200, text="OK"))

    outcome = await checker.check(_request(expected_body_contains="ok"))

    assert outcome.success is False


async def test_a_wrong_status_is_reported_ahead_of_the_body():
    """The status is the more useful diagnosis when both assertions fail."""
    checker = _checker(lambda request: httpx.Response(500, text="ok"))

    outcome = await checker.check(_request(expected_body_contains="not here"))

    assert outcome.success is False
    assert "expected status 200, got 500" in outcome.error_message


async def test_a_matching_body_does_not_rescue_a_wrong_status():
    checker = _checker(lambda request: httpx.Response(503, text="ok"))

    outcome = await checker.check(_request(expected_body_contains="ok"))

    assert outcome.success is False


async def test_an_empty_body_fails_an_assertion():
    checker = _checker(lambda request: httpx.Response(200, text=""))

    outcome = await checker.check(_request(expected_body_contains="ok"))

    assert outcome.success is False


async def test_a_body_with_an_unrecognised_charset_still_gets_matched():
    """httpx falls back to utf-8 (with replacement characters) for a charset

    it doesn't recognise, rather than raising, so this never reaches the
    decode-error handling in `_body_text` -- see the test below for that.
    """

    def handler(request):
        return httpx.Response(
            200,
            content=b"\xff\xfe\x00 garbled",
            headers={"content-type": "text/plain; charset=not-a-real-charset"},
        )

    checker = _checker(handler)

    outcome = await checker.check(_request(expected_body_contains="ok"))

    assert outcome.success is False
    assert "does not contain" in outcome.error_message
    assert outcome.status_code == 200


async def test_a_body_httpx_itself_cannot_decode_fails_the_assertion_rather_than_raising(
    monkeypatch,
):
    """`_body_text`'s except clause, forced rather than reached naturally.

    httpx's own `.text` is deliberately lenient -- it replaces bad bytes and
    falls back to utf-8 for a charset it doesn't recognise -- so nothing we
    can put in a response body or its headers actually makes it raise.
    `_body_text` still guards against `UnicodeDecodeError`/`LookupError` in
    case that ever stops being true, or a future httpx (or another HTTP
    client) is less forgiving; monkeypatching `.text` is the only way to
    exercise that guard today.
    """

    def _raise_decode_error(self):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(httpx.Response, "text", property(_raise_decode_error))

    checker = _checker(lambda request: httpx.Response(200, content=b"anything"))

    outcome = await checker.check(_request(expected_body_contains="ok"))

    assert outcome.success is False
    assert "does not contain" in outcome.error_message
    assert outcome.status_code == 200


def test_config_accepts_the_assertion_on_an_http_monitor():
    config = MonitorConfig(
        name="site", type="http", target="https://example.com", expected_body_contains="ok"
    )

    assert config.expected_body_contains == "ok"


@pytest.mark.parametrize("monitor_type,target", [("tcp", "db:5432"), ("dns", "example.com")])
def test_config_rejects_the_assertion_on_a_type_with_no_body(monitor_type, target):
    with pytest.raises(ValueError, match="never reads a response body"):
        MonitorConfig(
            name="thing", type=monitor_type, target=target, expected_body_contains="ok"
        )


def test_config_rejects_an_empty_assertion():
    """An empty substring is in every body, so it asserts nothing."""
    with pytest.raises(ValueError):
        MonitorConfig(
            name="site", type="http", target="https://example.com", expected_body_contains=""
        )


def test_load_config_surfaces_the_type_mismatch(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "monitors:\n"
        "  - name: db\n"
        "    type: tcp\n"
        "    target: db.example.com:5432\n"
        "    expected_body_contains: ok\n"
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
                    expected_body_contains="healthy",
                )
            ],
        )

    with db.session() as session:
        assert session.query(Monitor).one().expected_body_contains == "healthy"


def test_sync_monitors_clears_an_assertion_removed_from_the_config(db):
    with db.session() as session:
        sync_monitors(
            session,
            [
                MonitorConfig(
                    name="site",
                    type="http",
                    target="https://example.com",
                    expected_body_contains="healthy",
                )
            ],
        )

    with db.session() as session:
        sync_monitors(
            session, [MonitorConfig(name="site", type="http", target="https://example.com")]
        )

    with db.session() as session:
        assert session.query(Monitor).one().expected_body_contains is None


def test_check_request_carries_the_assertion_from_the_monitor(db):
    with db.session() as session:
        sync_monitors(
            session,
            [
                MonitorConfig(
                    name="site",
                    type="http",
                    target="https://example.com",
                    expected_body_contains="healthy",
                )
            ],
        )

    with db.session() as session:
        request = CheckRequest.from_monitor(session.query(Monitor).one())

    assert request.expected_body_contains == "healthy"
