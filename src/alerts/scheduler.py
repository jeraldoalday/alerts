"""Runs each monitor's check on its own interval and persists results.

Concurrency is asyncio-based: one long-lived task per monitor, sleeping
between checks on that monitor's own `interval_seconds`, bounded overall by
a semaphore sized from `scheduler.max_concurrent_checks` in the config.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, Iterable, List, Optional

from alerts.checkers.base import Checker, CheckOutcome, CheckRequest
from alerts.checkers.dns import DnsChecker
from alerts.checkers.http import HttpChecker
from alerts.checkers.ssl import SslChecker
from alerts.checkers.tcp import TcpChecker
from alerts.config import MonitorConfig
from alerts.db import Database
from alerts.incidents import sync_incident
from alerts.models import (
    CheckResult,
    Incident,
    IncidentState,
    Monitor,
    MonitorState,
    MonitorType,
    NotificationLog,
)
from alerts.notifications import decide_notifications
from alerts.notifiers.base import Notifier
from alerts.notifiers.message import format_message
from alerts.timeutil import utcnow

logger = logging.getLogger(__name__)


def default_checkers() -> Dict[MonitorType, Checker]:
    return {
        MonitorType.HTTP: HttpChecker(),
        MonitorType.TCP: TcpChecker(),
        MonitorType.DNS: DnsChecker(),
        MonitorType.SSL: SslChecker(),
    }


def exceeds_latency_budget(monitor: Monitor, outcome: CheckOutcome) -> bool:
    """Whether a successful check was slow enough to count as degraded.

    A monitor with no `degraded_latency_ms` has no budget to exceed, and a
    checker that reports no latency at all (a config error surfaced before
    any connection was attempted, say) can't be judged slow.
    """
    return (
        monitor.degraded_latency_ms is not None
        and outcome.latency_ms is not None
        and outcome.latency_ms >= monitor.degraded_latency_ms
    )


def apply_check_outcome(monitor: Monitor, outcome: CheckOutcome) -> None:
    """Update a monitor's streak counters and derived state from one check outcome.

    Crossing `failure_threshold` consecutive failures marks it down; crossing
    `recovery_threshold` consecutive successes marks it healthy again. A
    "healthy" check is UP normally, but DEGRADED when it came back slower
    than the monitor's `degraded_latency_ms` budget.

    Sliding from UP into DEGRADED takes effect on a single slow check, since
    degradation is a warning worth surfacing promptly and the monitor is
    still serving. Every other move into a healthy state waits for
    `recovery_threshold` consecutive supporting checks, so a target sitting
    on its latency budget doesn't flap.

    Which streak counts depends on the move. Coming back from DOWN or UNKNOWN
    is about the check passing at all, so it reads `consecutive_successes`.
    Leaving DEGRADED is about latency, so it reads `consecutive_fast_checks`:
    a monitor that has been slow for hours has a long success streak already
    banked, and spending it on the first fast check would flip the state back
    on a single sample -- exactly the flapping `recovery_threshold` exists to
    prevent.

    Incident records are not created here -- that's `sync_incident`'s job.
    """
    if outcome.success:
        monitor.consecutive_successes += 1
        monitor.consecutive_failures = 0
        is_slow = exceeds_latency_budget(monitor, outcome)
        monitor.consecutive_fast_checks = 0 if is_slow else monitor.consecutive_fast_checks + 1
        healthy_state = MonitorState.DEGRADED if is_slow else MonitorState.UP
        if monitor.state == healthy_state:
            return
        if healthy_state == MonitorState.DEGRADED and monitor.state == MonitorState.UP:
            monitor.state = MonitorState.DEGRADED
        elif monitor.state == MonitorState.DEGRADED:
            if monitor.consecutive_fast_checks >= monitor.recovery_threshold:
                monitor.state = MonitorState.UP
        elif monitor.consecutive_successes >= monitor.recovery_threshold:
            monitor.state = healthy_state
    else:
        monitor.consecutive_failures += 1
        monitor.consecutive_successes = 0
        monitor.consecutive_fast_checks = 0
        if (
            monitor.state != MonitorState.DOWN
            and monitor.consecutive_failures >= monitor.failure_threshold
        ):
            monitor.state = MonitorState.DOWN


def _disable_monitor(session, monitor: Monitor) -> None:
    """Stop checking a monitor that the config no longer declares.

    Its rows stay: the check and incident history is the reason the row isn't
    just deleted. Any incident still open is resolved on the way out, because
    nothing will ever check the monitor again to resolve it -- it would sit on
    the dashboard's open-incident count forever, and `alerts status` would
    report an outage that stopped being monitored rather than one still
    happening.
    """
    monitor.enabled = False
    open_incidents = (
        session.query(Incident)
        .filter_by(monitor_id=monitor.id, state=IncidentState.OPEN)
        .all()
    )
    for incident in open_incidents:
        incident.state = IncidentState.RESOLVED
        incident.resolved_at = utcnow()


def sync_monitors(session, monitor_configs: Iterable[MonitorConfig]) -> None:
    """Insert/update Monitor rows so they match the configured set.

    Existing rows are updated in place (preserving state/streak history).
    Monitors no longer present in the config are disabled rather than
    deleted, since deleting would cascade-delete their check/incident
    history -- but they are disabled rather than left alone, because a row
    the config no longer mentions was still being picked up by the scheduler
    and checked (and alerted on) indefinitely.

    A monitor that comes back into the config is re-enabled, so removing an
    entry and putting it back is reversible and keeps the old history.
    """
    existing = {m.name: m for m in session.query(Monitor).all()}
    configured = set()
    for cfg in monitor_configs:
        configured.add(cfg.name)
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
                    degraded_latency_ms=cfg.degraded_latency_ms,
                    expected_body_contains=cfg.expected_body_contains,
                    channels=list(cfg.channels),
                    enabled=True,
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
            monitor.degraded_latency_ms = cfg.degraded_latency_ms
            monitor.expected_body_contains = cfg.expected_body_contains
            monitor.channels = list(cfg.channels)
            monitor.enabled = True

    for name, monitor in existing.items():
        if name not in configured and monitor.enabled:
            _disable_monitor(session, monitor)


class Scheduler:
    """Runs each monitor's check on its own interval, bounded by a
    concurrency limit, and persists results to the database.
    """

    def __init__(
        self,
        database: Database,
        checkers: Optional[Dict[MonitorType, Checker]] = None,
        max_concurrent_checks: int = 20,
        notifiers: Optional[Dict[str, Notifier]] = None,
        cooldown_seconds: int = 900,
    ):
        self._database = database
        self._checkers = checkers or default_checkers()
        self._notifiers = notifiers or {}
        self._cooldown_seconds = cooldown_seconds
        self._semaphore = asyncio.Semaphore(max_concurrent_checks)
        self._stopping = asyncio.Event()
        self._tasks: List[asyncio.Task] = []

    async def run(self) -> None:
        with self._database.session() as session:
            monitor_ids = [
                m.id for m in session.query(Monitor).filter_by(enabled=True).all()
            ]

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
        for notifier in self._notifiers.values():
            aclose = getattr(notifier, "aclose", None)
            if aclose is not None:
                await aclose()

    async def _run_monitor_loop(self, monitor_id: int) -> None:
        while not self._stopping.is_set():
            try:
                await self._run_one_check(monitor_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                # One monitor's bad check must not take the process down.
                # `run()` gathers every monitor loop, so an exception escaping
                # here would propagate out of the gather and stop monitoring
                # everything else too -- the worst possible failure mode for a
                # tool whose job is noticing outages. Log it and try again on
                # the next interval instead.
                logger.exception("check failed unexpectedly for monitor id=%s", monitor_id)

            with self._database.session() as session:
                monitor = session.get(Monitor, monitor_id)
                interval = monitor.interval_seconds if monitor and monitor.enabled else None
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
                if monitor is None or not monitor.enabled:
                    return
                checker = self._checkers[monitor.type]
                request = CheckRequest.from_monitor(monitor)

            outcome = await checker.check(request)

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

        await self._dispatch_notifications(monitor_id)

    async def _dispatch_notifications(self, monitor_id: int) -> None:
        """Send whatever notifications the engine decides are due, and log them.

        Runs after every check (not just on state transitions), since
        escalation decisions depend on cooldown elapsing over repeated
        checks of an incident that's stayed open. Notifier sends happen
        outside the DB session -- they're network I/O -- so the incident's
        data needed for `decide_notifications` and message rendering is
        pulled out first.
        """
        if not self._notifiers:
            return

        with self._database.session() as session:
            monitor = session.get(Monitor, monitor_id)
            if monitor is None:
                return
            incident = (
                session.query(Incident)
                .filter_by(monitor_id=monitor.id)
                .order_by(Incident.started_at.desc())
                .first()
            )
            if incident is None:
                return
            decisions = decide_notifications(
                incident,
                monitor.channels,
                self._cooldown_seconds,
                now=utcnow(),
            )
            if not decisions:
                return
            incident_id = incident.id
            messages = {
                decision.kind: format_message(monitor, incident, decision.kind)
                for decision in decisions
            }

        for decision in decisions:
            notifier = self._notifiers.get(decision.channel_name)
            if notifier is None:
                continue
            result = await notifier.send(messages[decision.kind])
            with self._database.session() as session:
                session.add(
                    NotificationLog(
                        incident_id=incident_id,
                        channel_name=decision.channel_name,
                        kind=decision.kind,
                        success=result.success,
                        error_message=result.error_message,
                    )
                )
