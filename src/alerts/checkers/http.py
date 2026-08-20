from __future__ import annotations

import time
from typing import Optional

import httpx

from alerts.checkers.base import CheckOutcome, CheckRequest


def _body_text(response: httpx.Response) -> str:
    """The response body as text, tolerating an undecodable one.

    `response.text` decodes using the charset the server declared; a body
    that lies about its encoding raises rather than returning something to
    match against. A body nothing can decode contains no substring anyone
    configured, so the empty string is the right answer, not a crash that
    would be reported as if the endpoint were down.
    """
    try:
        return response.text
    except (UnicodeDecodeError, LookupError):
        return ""


def _assertion_failure(request: CheckRequest, response: httpx.Response) -> Optional[str]:
    """Why the response fails the request's assertions, or None if it passes.

    The status code is checked first: a 500 whose body happens to contain the
    expected substring is more usefully reported as the wrong status than as
    a body mismatch it doesn't have.
    """
    if response.status_code != request.expected_status:
        return f"expected status {request.expected_status}, got {response.status_code}"
    expected_body = request.expected_body_contains
    if expected_body is not None and expected_body not in _body_text(response):
        return f"response body does not contain {expected_body!r}"
    return None


class HttpChecker:
    """Checks an HTTP(S) endpoint by issuing a request and asserting on the response.

    The method defaults to GET and the request carries whatever headers the
    monitor configured, which is what makes a health endpoint behind an API
    token checkable at all.

    The status code is always compared. A request that also carries
    `expected_body_contains` has to find that substring in the response body:
    plenty of health endpoints answer 200 with a body saying which dependency
    is broken, and a status-only check calls that healthy.

    Reuses one `httpx.AsyncClient` (connection pooling) across calls to `check`
    unless a client is injected, e.g. for tests using `httpx.MockTransport`.
    """

    def __init__(self, client: Optional[httpx.AsyncClient] = None):
        self._client = client or httpx.AsyncClient(follow_redirects=True)
        self._owns_client = client is None

    async def check(self, request: CheckRequest) -> CheckOutcome:
        timeout_seconds = request.timeout_seconds
        start = time.monotonic()
        try:
            response = await self._client.request(
                request.method,
                request.target,
                timeout=timeout_seconds,
                headers=dict(request.headers),
            )
        except httpx.TimeoutException:
            return CheckOutcome(success=False, error_message=f"timed out after {timeout_seconds}s")
        except httpx.HTTPError as exc:
            return CheckOutcome(success=False, error_message=str(exc) or type(exc).__name__)

        latency_ms = (time.monotonic() - start) * 1000
        error_message = _assertion_failure(request, response)
        return CheckOutcome(
            success=error_message is None,
            latency_ms=latency_ms,
            status_code=response.status_code,
            error_message=error_message,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
