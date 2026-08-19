import asyncio

from alerts.checkers.base import CheckRequest
from alerts.checkers.tcp import TcpChecker


async def test_tcp_checker_succeeds_when_port_is_open():
    async def handle(reader, writer):
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]

    try:
        checker = TcpChecker()
        outcome = await checker.check(CheckRequest(target=f"{host}:{port}", timeout_seconds=2))
    finally:
        server.close()
        await server.wait_closed()

    assert outcome.success is True
    assert outcome.latency_ms is not None
    assert outcome.error_message is None


async def test_tcp_checker_fails_when_port_is_closed():
    checker = TcpChecker()
    # Port 1 is privileged and essentially never listening on localhost.
    outcome = await checker.check(CheckRequest(target="127.0.0.1:1", timeout_seconds=2))

    assert outcome.success is False
    assert outcome.error_message is not None


async def test_tcp_checker_rejects_malformed_target():
    checker = TcpChecker()
    outcome = await checker.check(CheckRequest(target="not-a-host-port", timeout_seconds=2))

    assert outcome.success is False
    assert "host:port" in outcome.error_message
