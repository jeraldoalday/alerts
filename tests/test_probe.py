"""Ad-hoc probing of the configured monitors, and the table it renders.

The contract that matters most here is the negative one: a probe runs the
real checks and writes nothing. Everything else -- ordering, concurrency,
the slow/failed classification -- is in service of a report an operator can
trust while editing a config against a live install.
"""
import asyncio

import pytest

from alerts.checkers.base import CheckOutcome, CheckRequest
from alerts.config import MonitorConfig
from alerts.models import MonitorType
from alerts.probe import ProbeResult, probe_monitors, select_monitors
from alerts.reporting import (
    collect_probe_rows,
    format_probe_summary,
    format_probe_table,
)


class RecordingChecker:
    """A checker that returns a scripted outcome and remembers its requests."""

    def __init__(self, outcome: CheckOutcome, delay: float = 0.0):
        self.outcome = outcome
        self.delay = delay
        self.requests = []
        self.closed = False

    async def check(self, request: CheckRequest) -> CheckOutcome:
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.outcome

    async def aclose(self) -> None:
        self.closed = True


def _monitor(name: str = "api", **overrides) -> MonitorConfig:
    fields = {"name": name, "type": "http", "target": "https://example.com"}
    fields.update(overrides)
    return MonitorConfig(**fields)


def _checkers(outcome: CheckOutcome, **kwargs):
    checker = RecordingChecker(outcome, **kwargs)
    return {monitor_type: checker for monitor_type in MonitorType}, checker


async def test_a_passing_check_is_reported_as_passed():
    checkers, _ = _checkers(CheckOutcome(success=True, latency_ms=42.0, status_code=200))

    results = await probe_monitors([_monitor()], checkers=checkers)

    assert len(results) == 1
    assert results[0].passed
    assert not results[0].is_slow
    assert results[0].detail == ""


async def test_a_failing_check_carries_the_checkers_own_message():
    checkers, _ = _checkers(
        CheckOutcome(success=False, error_message="expected status 200, got 502")
    )

    results = await probe_monitors([_monitor()], checkers=checkers)

    assert not results[0].passed
    assert results[0].detail == "expected status 200, got 502"


async def test_a_pass_at_or_above_the_budget_is_slow_rather_than_failed():
    checkers, _ = _checkers(CheckOutcome(success=True, latency_ms=1500.0))

    results = await probe_monitors(
        [_monitor(degraded_latency_ms=1500, timeout_seconds=10)], checkers=checkers
    )

    assert results[0].passed
    assert results[0].is_slow
    assert "1500ms budget" in results[0].detail


async def test_a_pass_below_the_budget_is_not_slow():
    checkers, _ = _checkers(CheckOutcome(success=True, latency_ms=1499.0))

    results = await probe_monitors(
        [_monitor(degraded_latency_ms=1500, timeout_seconds=10)], checkers=checkers
    )

    assert not results[0].is_slow


async def test_a_failure_is_never_reported_as_slow():
    """A check that failed slowly is a failure; "slow" would bury the reason."""
    checkers, _ = _checkers(
        CheckOutcome(success=False, latency_ms=9000.0, error_message="boom")
    )

    results = await probe_monitors(
        [_monitor(degraded_latency_ms=1500, timeout_seconds=10)], checkers=checkers
    )

    assert not results[0].is_slow
    assert results[0].detail == "boom"


async def test_the_request_carries_the_configured_method_and_headers(monkeypatch):
    monkeypatch.setenv("PROBE_TOKEN", "s3cret")
    checkers, checker = _checkers(CheckOutcome(success=True, latency_ms=5.0))

    await probe_monitors(
        [
            _monitor(
                method="HEAD",
                headers={"Authorization": "Bearer ${PROBE_TOKEN}"},
                expected_status=204,
            )
        ],
        checkers=checkers,
    )

    request = checker.requests[0]
    assert request.method == "HEAD"
    assert request.headers == {"Authorization": "Bearer s3cret"}
    assert request.expected_status == 204


async def test_an_unresolvable_placeholder_fails_the_check_naming_the_variable(monkeypatch):
    """The most useful thing a probe catches: valid config, healthy target,
    and a variable that isn't set where the checks would run."""
    monkeypatch.delenv("PROBE_MISSING", raising=False)
    checkers, checker = _checkers(CheckOutcome(success=True, latency_ms=5.0))

    results = await probe_monitors(
        [_monitor(headers={"Authorization": "Bearer ${PROBE_MISSING}"})], checkers=checkers
    )

    assert not results[0].passed
    assert "PROBE_MISSING" in results[0].detail
    assert checker.requests == [], "no request should be attempted"


async def test_each_monitor_type_goes_to_its_own_checker():
    outcome = CheckOutcome(success=True, latency_ms=1.0)
    checkers = {monitor_type: RecordingChecker(outcome) for monitor_type in MonitorType}

    await probe_monitors(
        [
            _monitor("web", type="http", target="https://example.com"),
            _monitor("port", type="tcp", target="example.com:443"),
            _monitor("name", type="dns", target="example.com"),
            _monitor("cert", type="ssl", target="example.com:443"),
        ],
        checkers=checkers,
    )

    assert [len(checkers[t].requests) for t in MonitorType] == [1, 1, 1, 1]


async def test_results_come_back_in_config_order_not_completion_order():
    """Two runs of the same config have to be diffable."""
    outcome = CheckOutcome(success=True, latency_ms=1.0)
    slow = RecordingChecker(outcome, delay=0.05)
    fast = RecordingChecker(outcome)
    checkers = {MonitorType.HTTP: slow, MonitorType.DNS: fast}

    results = await probe_monitors(
        [_monitor("slow-one", type="http"), _monitor("fast-one", type="dns", target="e.com")],
        checkers=checkers,
    )

    assert [result.name for result in results] == ["slow-one", "fast-one"]


async def test_concurrency_is_bounded_by_the_limit():
    running = 0
    peak = 0

    class CountingChecker:
        async def check(self, request):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= 1
            return CheckOutcome(success=True, latency_ms=1.0)

    checkers = {monitor_type: CountingChecker() for monitor_type in MonitorType}

    await probe_monitors(
        [_monitor(f"m{index}") for index in range(8)], checkers=checkers, max_concurrent=2
    )

    assert peak <= 2


async def test_injected_checkers_are_left_open_for_their_owner_to_close():
    checkers, checker = _checkers(CheckOutcome(success=True, latency_ms=1.0))

    await probe_monitors([_monitor()], checkers=checkers)

    assert not checker.closed


async def test_checkers_it_created_itself_are_closed(monkeypatch):
    """An HTTP checker owns a connection pool; a command that exits leaks it."""
    checkers, checker = _checkers(CheckOutcome(success=True, latency_ms=1.0))
    monkeypatch.setattr("alerts.probe.default_checkers", lambda: checkers)

    await probe_monitors([_monitor()])

    assert checker.closed


async def test_no_monitors_is_an_empty_run_rather_than_an_error():
    assert await probe_monitors([]) == []


def test_select_monitors_returns_everything_by_default():
    monitors = [_monitor("a"), _monitor("b")]

    assert select_monitors(monitors) == monitors


def test_select_monitors_narrows_to_one_name():
    monitors = [_monitor("a"), _monitor("b")]

    assert [m.name for m in select_monitors(monitors, monitor="b")] == ["b"]


def test_select_monitors_yields_nothing_for_an_unknown_name():
    assert select_monitors([_monitor("a")], monitor="zzz") == []


def _row(name: str, outcome: CheckOutcome, budget=None) -> ProbeResult:
    return ProbeResult(
        name=name,
        type="http",
        target="https://example.com",
        outcome=outcome,
        degraded_latency_ms=budget,
    )


def test_the_table_shows_the_target_that_was_actually_asked():
    rows = collect_probe_rows([_row("api", CheckOutcome(success=True, latency_ms=87.0))])

    table = format_probe_table(rows)

    assert "https://example.com" in table
    assert "87ms" in table


@pytest.mark.parametrize(
    ("outcome", "budget", "expected"),
    [
        (CheckOutcome(success=True, latency_ms=10.0), None, "ok"),
        (CheckOutcome(success=True, latency_ms=2000.0), 1500, "slow"),
        (CheckOutcome(success=False, error_message="down"), None, "FAIL"),
    ],
)
def test_each_outcome_gets_its_own_result_word(outcome, budget, expected):
    rows = collect_probe_rows([_row("api", outcome, budget)])

    assert rows[0].result == expected


def test_only_a_failure_is_shouted():
    """The one value anyone scanning a long table is looking for."""
    rows = collect_probe_rows(
        [
            _row("a", CheckOutcome(success=True, latency_ms=1.0)),
            _row("b", CheckOutcome(success=False, error_message="x")),
        ]
    )

    assert [row.result.isupper() for row in rows] == [False, True]


def test_a_missing_latency_renders_as_a_dash():
    rows = collect_probe_rows([_row("api", CheckOutcome(success=False, error_message="x"))])

    assert "-" in format_probe_table(rows)


def test_a_long_detail_is_truncated_rather_than_wrapping_the_table():
    rows = collect_probe_rows(
        [_row("api", CheckOutcome(success=False, error_message="x" * 200))]
    )

    line = [line for line in format_probe_table(rows).splitlines() if line.startswith("api")][0]
    assert "…" in line
    assert len(line) < 160


def test_the_summary_counts_partition_the_rows():
    rows = collect_probe_rows(
        [
            _row("ok1", CheckOutcome(success=True, latency_ms=1.0)),
            _row("ok2", CheckOutcome(success=True, latency_ms=1.0)),
            _row("slow", CheckOutcome(success=True, latency_ms=2000.0), 1500),
            _row("bad", CheckOutcome(success=False, error_message="x")),
        ]
    )

    assert format_probe_summary(rows) == "4 monitor(s) checked, 2 ok, 1 slow, 1 failed"


def test_the_summary_omits_slow_when_nothing_was_slow():
    rows = collect_probe_rows([_row("ok", CheckOutcome(success=True, latency_ms=1.0))])

    assert format_probe_summary(rows) == "1 monitor(s) checked, 1 ok, 0 failed"


def test_an_empty_run_renders_a_sentence_rather_than_a_header_row():
    assert format_probe_table([]) == "No monitors to check."
    assert format_probe_summary([]) == "0 monitor(s) checked"
