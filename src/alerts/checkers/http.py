from __future__ import annotations

import time
from typing import Optional

import httpx

from alerts.checkers.base import CheckOutcome, CheckRequest


class HttpChecker:
    """Checks an HTTP(S) endpoint by issuing a GET and comparing the status code.

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
            response = await self._client.get(request.target, timeout=timeout_seconds)
        except httpx.TimeoutException:
            return CheckOutcome(success=False, error_message=f"timed out after {timeout_seconds}s")
        except httpx.HTTPError as exc:
            return CheckOutcome(success=False, error_message=str(exc) or type(exc).__name__)

        latency_ms = (time.monotonic() - start) * 1000
        success = response.status_code == request.expected_status
        error_message = (
            None
            if success
            else f"expected status {request.expected_status}, got {response.status_code}"
        )
        return CheckOutcome(
            success=success,
            latency_ms=latency_ms,
            status_code=response.status_code,
            error_message=error_message,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
