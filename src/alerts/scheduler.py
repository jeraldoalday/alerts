"""Runs each monitor's check on its own interval and persists results.

Concurrency is asyncio-based: one long-lived task per monitor, sleeping
between checks on that monitor's own `interval_seconds`, bounded overall by
a semaphore sized from `scheduler.max_concurrent_checks` in the config.
"""
from __future__ import annotations

import asyncio
from typing import Dict, Iterable, List, Optional

from alerts.checkers.base import Checker, CheckOutcome
from alerts.checkers.dns import DnsChecker
from alerts.checkers.http import HttpChecker
from alerts.checkers.ssl import SslChecker
from alerts.checkers.tcp import TcpChecker
from alerts.config import MonitorConfig
from alerts.db import Database
from alerts.incidents import sync_incident
from alerts.models import CheckResult, Monitor, MonitorState, MonitorType


def default_checkers() -> Dict[MonitorType, Checker]:
    return {
        MonitorType.HTTP: HttpChecker(),
        MonitorType.TCP: TcpChecker(),
        MonitorType.DNS: DnsChecker(),
        MonitorType.SSL: SslChecker(),
    }


def apply_check_outcome(monitor: Monitor, outcome: CheckOutcome) -> None:
    """Update a monitor's streak counters and derived state from one check outcome.

    Crossing `failure_threshold` consecutive failures marks it down; crossing
    `recovery_threshold` consecutive successes marks it up. Incident records
    are not created here -- that's the notification engine's job.
    """
    if outcome.success:
        monitor.consecutive_successes += 1
        monitor.consecutive_failures = 0
        if (
            monitor.state != MonitorState.UP
            and monitor.consecutive_successes >= monitor.recovery_threshold
        ):
            monitor.state = MonitorState.UP
    else:
        monitor.consecutive_failures += 1
        monitor.consecutive_successes = 0
        if (
            monitor.state != MonitorState.DOWN
            and monitor.consecutive_failures >= monitor.failure_threshold
        ):
            monitor.state = MonitorState.DOWN


def sync_monitors(session, monitor_configs: Iterable[MonitorConfig]) -> None:
    """Insert/update Monitor rows so they match the configured set.

    Existing rows are updated in place (preserving state/streak history);
    monitors no longer present in the config are left alone rather than
    deleted, since deleting would cascade-delete their check/incident history.
    """
    existing = {m.name: m for m in session.query(Monitor).all()}
    for cfg in monitor_configs:
        monitor = existing.get(cfg.name)
        if monitor is None:
            session.add(
                Monitor(
                    name=cfg.name,
                    type=MonitorType(cfg.type),
                    target=cfg.target,
                    interval_seconds=cfg.interval_seconds,
                    timeout_seconds=cfg.timeout_seconds,
                    expected_status=cfg.expected_status,
                    failure_threshold=cfg.failure_threshold,
                    recovery_threshold=cfg.recovery_threshold,
                )
            )
        else:
            monitor.type = MonitorType(cfg.type)
            monitor.target = cfg.target
            monitor.interval_seconds = cfg.interval_seconds
            monitor.timeout_seconds = cfg.timeout_seconds
            monitor.expected_status = cfg.expected_status
            monitor.failure_threshold = cfg.failure_threshold
            monitor.recovery_threshold = cfg.recovery_threshold


class Scheduler:
    """Runs each monitor's check on its own interval, bounded by a
    concurrency limit, and persists results to the database.
    """

    def __init__(
        self,
        database: Database,
        checkers: Optional[Dict[MonitorType, Checker]] = None,
        max_concurrent_checks: int = 20,
    ):
        self._database = database
        self._checkers = checkers or default_checkers()
        self._semaphore = asyncio.Semaphore(max_concurrent_checks)
        self._stopping = asyncio.Event()
        self._tasks: List[asyncio.Task] = []

    async def run(self) -> None:
        with self._database.session() as session:
            monitor_ids = [m.id for m in session.query(Monitor).all()]

        self._tasks = [asyncio.create_task(self._run_monitor_loop(mid)) for mid in monitor_ids]
        if self._tasks:
            await asyncio.gather(*self._tasks)

    async def stop(self) -> None:
        self._stopping.set()
        if self._tasks:
            await asyncio.gather(*self._tasks)
        for checker in self._checkers.values():
            aclose = getattr(checker, "aclose", None)
            if aclose is not None:
                await aclose()

    async def _run_monitor_loop(self, monitor_id: int) -> None:
        while not self._stopping.is_set():
            await self._run_one_check(monitor_id)

            with self._database.session() as session:
                monitor = session.get(Monitor, monitor_id)
                interval = monitor.interval_seconds if monitor else None
            if interval is None:
                return

            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _run_one_check(self, monitor_id: int) -> None:
        async with self._semaphore:
            with self._database.session() as session:
                monitor = session.get(Monitor, monitor_id)
                if monitor is None:
                    return
                checker = self._checkers[monitor.type]
                target = monitor.target
                timeout_seconds = monitor.timeout_seconds
                expected_status = monitor.expected_status

            outcome = await checker.check(
                target=target, timeout_seconds=timeout_seconds, expected_status=expected_status
            )

            with self._database.session() as session:
                monitor = session.get(Monitor, monitor_id)
                if monitor is None:
                    return
                session.add(
                    CheckResult(
                        monitor_id=monitor.id,
                        success=outcome.success,
                        latency_ms=outcome.latency_ms,
                        status_code=outcome.status_code,
                        error_message=outcome.error_message,
                    )
                )
                previous_state = monitor.state
                apply_check_outcome(monitor, outcome)
                sync_incident(session, monitor, previous_state, cause=outcome.error_message)
