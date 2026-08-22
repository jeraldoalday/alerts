"""Shared types for check implementations."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Mapping, Optional, Protocol

from alerts.envsubst import expand_mapping

if TYPE_CHECKING:
    from alerts.config import MonitorConfig
    from alerts.models import Monitor


@dataclass(frozen=True)
class CheckRequest:
    """Everything a checker needs to run one check against a target.

    Passed as a single object rather than as keyword arguments so that adding
    a per-protocol assertion doesn't ripple through every checker signature
    and every caller. A checker ignores the fields that don't apply to it --
    `expected_status` means nothing to a DNS lookup -- which is why the
    protocol-specific ones all carry defaults.
    """

    target: str
    timeout_seconds: float
    expected_status: int = 200
    expected_body_contains: Optional[str] = None
    method: str = "GET"
    headers: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_monitor(cls, monitor: "Monitor") -> "CheckRequest":
        """Build a request from a monitor row.

        Reads every field eagerly so the caller can build the request inside
        a database session and run the check itself outside of one.

        Header values are expanded here rather than on the way into the
        database, so a resolved secret exists only for the duration of one
        check. A placeholder that can't be resolved raises `MissingEnvVar`;
        the scheduler records that as a failed check naming the variable,
        which is a far better answer than sending "Bearer " and reporting
        whatever the target says about it.
        """
        return cls(
            target=monitor.target,
            timeout_seconds=monitor.timeout_seconds,
            expected_status=monitor.expected_status,
            expected_body_contains=monitor.expected_body_contains,
            method=monitor.method,
            headers=expand_mapping(monitor.headers or {}),
        )

    @classmethod
    def from_config(cls, monitor: "MonitorConfig") -> "CheckRequest":
        """Build a request straight from a config entry, with no database row.

        `alerts check` probes what the config declares, before -- or entirely
        without -- any of it being synced into a monitors table. The fields
        are the same ones `from_monitor` reads, because the row is a copy of
        the config entry; keeping both constructors here is what stops the
        two paths from drifting into checking subtly different requests.
        """
        return cls(
            target=monitor.target,
            timeout_seconds=monitor.timeout_seconds,
            expected_status=monitor.expected_status,
            expected_body_contains=monitor.expected_body_contains,
            method=monitor.method,
            headers=expand_mapping(monitor.headers),
        )


@dataclass(frozen=True)
class CheckOutcome:
    """The result of running one check against a monitor's target."""

    success: bool
    latency_ms: Optional[float] = None
    status_code: Optional[int] = None
    error_message: Optional[str] = None

    def exceeds_budget(self, budget_ms: Optional[float]) -> bool:
        """Whether this outcome was slow enough to count as degraded.

        A monitor with no budget has nothing to exceed, and an outcome with
        no latency at all -- a config error surfaced before any connection
        was attempted -- can't be judged slow. At or above the budget counts,
        so a budget of exactly 1500ms is a promise about staying under it.

        Lives on the outcome rather than beside the state machine because
        both the scheduler and `alerts check` ask the same question of the
        same object, and an ad-hoc probe calling a target slow on a different
        rule from the one the scheduler applies would be worse than useless.
        """
        return (
            budget_ms is not None
            and self.latency_ms is not None
            and self.latency_ms >= budget_ms
        )


class Checker(Protocol):
    """Runs a single check against a target and reports the outcome."""

    async def check(self, request: CheckRequest) -> CheckOutcome: ...
