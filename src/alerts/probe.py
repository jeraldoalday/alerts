"""Runs one check per configured monitor, immediately and without side effects.

The scheduler answers "is this service up" continuously, on its own
intervals, into a database. This answers "does this config do what I think
it does" once, right now, at a terminal -- the question asked while editing
`config.yaml`, and the one a deploy pipeline asks before shipping a change
to it.

Nothing here writes. No check results are recorded, no monitor state moves,
no incident opens, and nothing is notified. That is the whole point: a probe
has to be safe to run repeatedly against a live install, from a laptop,
while the real scheduler is running, without a burst of ad-hoc checks
reading as an outage or paging anyone. It also means `alerts check` works
against a config whose monitors have never been synced into a database at
all, which is exactly the state a new config is in.

The checks themselves are the same code paths the scheduler uses -- the same
checkers, the same `CheckRequest`, the same latency budget -- because a probe
that agreed with the config but not with the scheduler would be worse than
having no probe.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from alerts.checkers.base import Checker, CheckOutcome, CheckRequest
from alerts.config import MonitorConfig
from alerts.envsubst import MissingEnvVar
from alerts.models import MonitorType
from alerts.scheduler import default_checkers
from alerts.tags import normalise as normalise_tag


@dataclass(frozen=True)
class ProbeResult:
    """What one ad-hoc check found, and what the config expected of it."""

    name: str
    type: str
    target: str
    outcome: CheckOutcome
    degraded_latency_ms: Optional[float] = None

    @property
    def passed(self) -> bool:
        return self.outcome.success

    @property
    def is_slow(self) -> bool:
        """Whether a passing check came back at or above the latency budget.

        Reported separately from pass/fail because it is separate: the target
        answered correctly, and the scheduler seeing this same reading would
        mark the monitor degraded rather than down. Folding it into "fail"
        would have a probe disagree with the state machine it exists to
        predict; dropping it would hide the budget that is about to start
        firing.
        """
        return self.passed and self.outcome.exceeds_budget(self.degraded_latency_ms)

    @property
    def detail(self) -> str:
        """One line explaining the result, from the check itself where possible.

        A failure carries the checker's own message. A slow pass names the
        budget it crossed, since "1624ms" alone doesn't say what is wrong
        with it.
        """
        if self.outcome.error_message:
            return self.outcome.error_message
        if self.is_slow:
            return f"slower than the {self.degraded_latency_ms:.0f}ms budget"
        return ""


def select_monitors(
    monitors: Sequence[MonitorConfig],
    *,
    monitor: Optional[str] = None,
    tag: Optional[str] = None,
) -> List[MonitorConfig]:
    """The configured monitors a probe run should cover, in config order.

    Both filters narrow, so giving each of them keeps only the monitors that
    satisfy both -- `--monitor api --tag prod` checks `api` if it is tagged
    `prod` and nothing otherwise, rather than quietly ignoring one of the two
    things that was asked for.

    A filter that matches nothing yields an empty list rather than an error,
    matching how `alerts incidents --monitor` and the dashboard treat a name
    nothing matches. The caller reports it either way, and an empty result is
    the honest answer to "check the ones tagged `prod`" when none are.
    """
    selected = list(monitors)
    if monitor is not None:
        selected = [config for config in selected if config.name == monitor]
    if tag is not None:
        wanted = normalise_tag(tag)
        selected = [config for config in selected if wanted in config.tags]
    return selected


async def _probe_one(
    config: MonitorConfig,
    checkers: Dict[MonitorType, Checker],
    semaphore: asyncio.Semaphore,
) -> ProbeResult:
    """Run one monitor's check once and wrap the outcome.

    A `${VAR}` placeholder that can't be resolved is reported as a failed
    check naming the variable, exactly as the scheduler records it. That is
    the single most useful thing this command can catch: the config is valid,
    the target is fine, and the check would fail every interval because a
    variable isn't set in the environment the scheduler runs in.
    """
    async with semaphore:
        try:
            request = CheckRequest.from_config(config)
        except MissingEnvVar as exc:
            outcome = CheckOutcome(success=False, error_message=str(exc))
        else:
            outcome = await checkers[MonitorType(config.type)].check(request)

    return ProbeResult(
        name=config.name,
        type=config.type,
        target=config.target,
        outcome=outcome,
        degraded_latency_ms=config.degraded_latency_ms,
    )


async def probe_monitors(
    monitors: Sequence[MonitorConfig],
    *,
    checkers: Optional[Dict[MonitorType, Checker]] = None,
    max_concurrent: int = 20,
) -> List[ProbeResult]:
    """Check every given monitor once, concurrently, in config order.

    Results come back in the order the monitors were declared rather than in
    the order the targets happened to answer, so two runs of the same config
    produce diffable output.

    Checkers this function created are closed before returning -- an HTTP
    checker owns a connection pool, and a command that exits leaving one open
    logs a warning about it on the way out. Injected checkers are left alone:
    their owner closes them.
    """
    owned = checkers is None
    checkers = checkers or default_checkers()
    semaphore = asyncio.Semaphore(max_concurrent)
    try:
        return list(
            await asyncio.gather(
                *(_probe_one(config, checkers, semaphore) for config in monitors)
            )
        )
    finally:
        if owned:
            await close_checkers(checkers)


async def close_checkers(checkers: Dict[MonitorType, Checker]) -> None:
    """Close every checker that holds something worth closing."""
    for checker in checkers.values():
        aclose = getattr(checker, "aclose", None)
        if aclose is not None:
            await aclose()
