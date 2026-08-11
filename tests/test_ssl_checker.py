from datetime import datetime, timedelta, timezone

from alerts.checkers.ssl import SslChecker, _evaluate_certificate


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
    outcome = await checker.check(target="example.com:443", timeout_seconds=2, expected_status=200)

    assert outcome.success is False
    assert "connection refused" in outcome.error_message


async def test_ssl_checker_rejects_malformed_target():
    checker = SslChecker()

    outcome = await checker.check(
        target="example.com:notaport", timeout_seconds=2, expected_status=200
    )

    assert outcome.success is False
    assert "must be" in outcome.error_message


async def test_ssl_checker_uses_injected_certificate():
    not_after = datetime.now(timezone.utc) + timedelta(days=10)

    async def fake_fetch(host: str, port: int):
        return _cert(not_after)

    checker = SslChecker(fetch_certificate=fake_fetch)
    outcome = await checker.check(target="example.com", timeout_seconds=2, expected_status=200)

    assert outcome.success is True
