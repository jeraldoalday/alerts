from __future__ import annotations

import asyncio
import time
from typing import Tuple

from alerts.checkers.base import CheckOutcome, CheckRequest


def _split_host_port(target: str) -> Tuple[str, int]:
    """Split a monitor's TCP target into the host and port to connect to.

    A bracketed host ("[::1]:443") is unwrapped before anything is
    returned: the brackets are IPv6's own way of telling its own colons
    apart from the one separating the port, not part of the host
    `open_connection` expects. Passed through with the brackets still
    attached, every such target resolves to nothing and the monitor reports
    down -- forever, since there's no way to write it that would resolve
    instead.
    """
    if target.startswith("["):
        end = target.find("]")
        if end == -1:
            raise ValueError(f"TCP target must be 'host:port', got {target!r}")
        host, remainder = target[1:end], target[end + 1 :]
        if not remainder.startswith(":") or not remainder[1:].isdigit():
            raise ValueError(f"TCP target must be 'host:port', got {target!r}")
        return host, int(remainder[1:])

    host, _, port = target.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(f"TCP target must be 'host:port', got {target!r}")
    return host, int(port)


class TcpChecker:
    """Checks a TCP target ("host:port") by attempting to open a connection."""

    async def check(self, request: CheckRequest) -> CheckOutcome:
        try:
            host, port = _split_host_port(request.target)
        except ValueError as exc:
            return CheckOutcome(success=False, error_message=str(exc))

        start = time.monotonic()
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=request.timeout_seconds
            )
        except (OSError, asyncio.TimeoutError) as exc:
            return CheckOutcome(success=False, error_message=str(exc) or type(exc).__name__)

        latency_ms = (time.monotonic() - start) * 1000
        writer.close()
        await writer.wait_closed()
        return CheckOutcome(success=True, latency_ms=latency_ms)
