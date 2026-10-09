from __future__ import annotations

import asyncio
import ssl as ssl_module
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from alerts.checkers.base import CheckOutcome, CheckRequest

CertificateFetcher = Callable[[str, int], Awaitable[Dict[str, Any]]]


def _split_host_port(target: str, default_port: int = 443) -> Tuple[str, int]:
    """Split a monitor's SSL target into the host and port to connect to.

    A bracketed host ("[::1]:443", or bare "[::1]" for the default port) is
    unwrapped before anything is returned: the brackets are IPv6's own way
    of telling its own colons apart from the one separating the port, not
    part of the host `open_connection` expects. Passed through with the
    brackets still attached, every such target resolves to nothing and the
    monitor reports down -- forever, since there's no way to write it that
    would resolve instead.
    """
    if target.startswith("["):
        end = target.find("]")
        if end == -1:
            raise ValueError(f"SSL target must be 'host' or 'host:port', got {target!r}")
        host, remainder = target[1:end], target[end + 1 :]
        if not remainder:
            return host, default_port
        if not remainder.startswith(":") or not remainder[1:].isdigit():
            raise ValueError(f"SSL target must be 'host' or 'host:port', got {target!r}")
        return host, int(remainder[1:])

    host, sep, port = target.rpartition(":")
    if not sep:
        return target, default_port
    if not host or not port.isdigit():
        raise ValueError(f"SSL target must be 'host' or 'host:port', got {target!r}")
    return host, int(port)


async def _fetch_certificate(host: str, port: int) -> Dict[str, Any]:
    context = ssl_module.create_default_context()
    _, writer = await asyncio.open_connection(host, port, ssl=context, server_hostname=host)
    try:
        ssl_object = writer.get_extra_info("ssl_object")
        return ssl_object.getpeercert() if ssl_object else {}
    finally:
        writer.close()
        await writer.wait_closed()


def _evaluate_certificate(cert: Dict[str, Any], latency_ms: float) -> CheckOutcome:
    if not cert or "notAfter" not in cert:
        return CheckOutcome(
            success=False, latency_ms=latency_ms, error_message="certificate has no expiry info"
        )

    expires_at = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(
        tzinfo=timezone.utc
    )
    if expires_at <= datetime.now(timezone.utc):
        return CheckOutcome(
            success=False,
            latency_ms=latency_ms,
            error_message=f"certificate expired at {expires_at.isoformat()}",
        )
    return CheckOutcome(success=True, latency_ms=latency_ms)


class SslChecker:
    """Checks that a TLS endpoint presents a valid, unexpired certificate.

    Accepts an injectable certificate fetcher so tests can exercise expiry
    logic and connection failures without a real TLS handshake.
    """

    def __init__(self, fetch_certificate: Optional[CertificateFetcher] = None):
        self._fetch_certificate = fetch_certificate or _fetch_certificate

    async def check(self, request: CheckRequest) -> CheckOutcome:
        try:
            host, port = _split_host_port(request.target)
        except ValueError as exc:
            return CheckOutcome(success=False, error_message=str(exc))

        start = time.monotonic()
        try:
            cert = await asyncio.wait_for(
                self._fetch_certificate(host, port), timeout=request.timeout_seconds
            )
        except (OSError, ssl_module.SSLError, asyncio.TimeoutError) as exc:
            return CheckOutcome(success=False, error_message=str(exc) or type(exc).__name__)

        latency_ms = (time.monotonic() - start) * 1000
        return _evaluate_certificate(cert, latency_ms)
