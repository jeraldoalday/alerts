import asyncio
import socket

from alerts.checkers.dns import DnsChecker


async def test_dns_checker_succeeds_when_resolver_finds_a_record():
    async def fake_resolver(hostname: str):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    checker = DnsChecker(resolver=fake_resolver)
    outcome = await checker.check(target="example.com", timeout_seconds=2, expected_status=200)

    assert outcome.success is True
    assert outcome.latency_ms is not None


async def test_dns_checker_fails_when_resolution_errors():
    async def fake_resolver(hostname: str):
        raise socket.gaierror("Name or service not known")

    checker = DnsChecker(resolver=fake_resolver)
    outcome = await checker.check(
        target="this-domain-should-not-exist.invalid", timeout_seconds=2, expected_status=200
    )

    assert outcome.success is False
    assert "not known" in outcome.error_message


async def test_dns_checker_fails_on_timeout():
    async def slow_resolver(hostname: str):
        await asyncio.sleep(10)

    checker = DnsChecker(resolver=slow_resolver)
    outcome = await checker.check(target="example.com", timeout_seconds=0.05, expected_status=200)

    assert outcome.success is False
    assert "timed out" in outcome.error_message
