from datetime import datetime, timedelta, timezone

import pytest

from alerts.checkers.base import CheckRequest
from alerts.checkers.ssl import SslChecker, _evaluate_certificate, _split_host_port


def _cert(not_after: datetime) -> dict:
    return {"notAfter": not_after.strftime("%b %d %H:%M:%S %Y GMT")}


def test_evaluate_certificate_succeeds_when_not_expired():
    not_after = datetime.now(timezone.utc) + timedelta(days=30)

    outcome = _evaluate_certificate(_cert(not_after), latency_ms=12.0)

    assert outcome.success is True
    assert outcome.latency_ms == 12.0


def test_evaluate_certificate_fails_when_expired():
    not_after = datetime.now(timezone.utc) - timedelta(days=1)

    outcome = _evaluate_certificate(_cert(not_after), latency_ms=12.0)

    assert outcome.success is False
    assert "expired" in outcome.error_message


def test_evaluate_certificate_fails_when_no_expiry_present():
    outcome = _evaluate_certificate({}, latency_ms=12.0)

    assert outcome.success is False
    assert "expiry" in outcome.error_message


async def test_ssl_checker_reports_connection_errors():
    async def failing_fetch(host: str, port: int):
        raise ConnectionRefusedError("connection refused")

    checker = SslChecker(fetch_certificate=failing_fetch)
    outcome = await checker.check(CheckRequest(target="example.com:443", timeout_seconds=2))

    assert outcome.success is False
    assert "connection refused" in outcome.error_message


async def test_ssl_checker_rejects_malformed_target():
    checker = SslChecker()

    outcome = await checker.check(
        CheckRequest(target="example.com:notaport", timeout_seconds=2)
    )

    assert outcome.success is False
    assert "must be" in outcome.error_message


async def test_ssl_checker_uses_injected_certificate():
    not_after = datetime.now(timezone.utc) + timedelta(days=10)

    async def fake_fetch(host: str, port: int):
        return _cert(not_after)

    checker = SslChecker(fetch_certificate=fake_fetch)
    outcome = await checker.check(CheckRequest(target="example.com", timeout_seconds=2))

    assert outcome.success is True


async def test_ssl_checker_unwraps_a_bracketed_ipv6_host():
    # The brackets are IPv6's own way of telling its colons apart from the
    # one separating the port -- left on, the host handed to the fetcher
    # would be "[::1]", which resolves to nothing.
    seen = {}

    async def fake_fetch(host: str, port: int):
        seen["host"], seen["port"] = host, port
        return _cert(datetime.now(timezone.utc) + timedelta(days=10))

    checker = SslChecker(fetch_certificate=fake_fetch)
    outcome = await checker.check(CheckRequest(target="[::1]:8443", timeout_seconds=2))

    assert outcome.success is True
    assert seen == {"host": "::1", "port": 8443}


def test_split_host_port_defaults_a_bare_bracketed_host_to_443():
    assert _split_host_port("[::1]") == ("::1", 443)


def test_split_host_port_rejects_an_unterminated_bracket():
    with pytest.raises(ValueError, match="must be"):
        _split_host_port("[::1")


def test_split_host_port_rejects_a_non_numeric_port_after_a_bracketed_host():
    with pytest.raises(ValueError, match="must be"):
        _split_host_port("[::1]:https")
