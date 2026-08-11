import httpx

from alerts.checkers.http import HttpChecker


def _checker(handler) -> HttpChecker:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    return HttpChecker(client=client)


async def test_http_checker_success_on_expected_status():
    checker = _checker(lambda request: httpx.Response(200))

    outcome = await checker.check(target="https://example.com", timeout_seconds=5, expected_status=200)

    assert outcome.success is True
    assert outcome.status_code == 200
    assert outcome.latency_ms is not None
    assert outcome.error_message is None


async def test_http_checker_fails_on_unexpected_status():
    checker = _checker(lambda request: httpx.Response(503))

    outcome = await checker.check(target="https://example.com", timeout_seconds=5, expected_status=200)

    assert outcome.success is False
    assert outcome.status_code == 503
    assert outcome.error_message is not None
    assert "503" in outcome.error_message


async def test_http_checker_reports_connection_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    checker = _checker(handler)

    outcome = await checker.check(target="https://example.com", timeout_seconds=5, expected_status=200)

    assert outcome.success is False
    assert outcome.status_code is None
    assert "connection refused" in outcome.error_message


async def test_http_checker_reports_timeouts():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    checker = _checker(handler)

    outcome = await checker.check(target="https://example.com", timeout_seconds=5, expected_status=200)

    assert outcome.success is False
    assert "timed out" in outcome.error_message


async def test_aclose_closes_a_self_owned_client():
    checker = HttpChecker()

    await checker.aclose()

    assert checker._client.is_closed is True


async def test_aclose_leaves_an_injected_client_open():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    checker = HttpChecker(client=client)

    await checker.aclose()

    assert client.is_closed is False
    await client.aclose()
