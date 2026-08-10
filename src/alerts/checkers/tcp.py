from __future__ import annotations

import asyncio
import time
from typing import Tuple

from alerts.checkers.base import CheckOutcome


def _split_host_port(target: str) -> Tuple[str, int]:
    host, _, port = target.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(f"TCP target must be 'host:port', got {target!r}")
    return host, int(port)


class TcpChecker:
    """Checks a TCP target ("host:port") by attempting to open a connection."""

    async def check(
        self, *, target: str, timeout_seconds: float, expected_status: int
    ) -> CheckOutcome:
        try:
            host, port = _split_host_port(target)
        except ValueError as exc:
            return CheckOutcome(success=False, error_message=str(exc))

        start = time.monotonic()
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout_seconds
            )
        except (OSError, asyncio.TimeoutError) as exc:
            return CheckOutcome(success=False, error_message=str(exc) or type(exc).__name__)

        latency_ms = (time.monotonic() - start) * 1000
        writer.close()
        await writer.wait_closed()
        return CheckOutcome(success=True, latency_ms=latency_ms)
