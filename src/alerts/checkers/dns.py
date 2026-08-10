from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Optional

from alerts.checkers.base import CheckOutcome

Resolver = Callable[[str], Awaitable[Any]]


async def _default_resolver(hostname: str) -> Any:
    loop = asyncio.get_running_loop()
    return await loop.getaddrinfo(hostname, None)


class DnsChecker:
    """Checks that a hostname resolves via DNS.

    Accepts an injectable resolver (defaults to `getaddrinfo` on the running
    loop) so tests can exercise success/failure paths without real DNS.
    """

    def __init__(self, resolver: Optional[Resolver] = None):
        self._resolver = resolver or _default_resolver

    async def check(
        self, *, target: str, timeout_seconds: float, expected_status: int
    ) -> CheckOutcome:
        start = time.monotonic()
        try:
            await asyncio.wait_for(self._resolver(target), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            return CheckOutcome(
                success=False, error_message=f"DNS lookup timed out after {timeout_seconds}s"
            )
        except OSError as exc:
            return CheckOutcome(success=False, error_message=str(exc) or type(exc).__name__)

        latency_ms = (time.monotonic() - start) * 1000
        return CheckOutcome(success=True, latency_ms=latency_ms)
