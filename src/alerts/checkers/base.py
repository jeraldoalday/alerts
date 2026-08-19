"""Shared types for check implementations."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Protocol

if TYPE_CHECKING:
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

    @classmethod
    def from_monitor(cls, monitor: "Monitor") -> "CheckRequest":
        """Build a request from a monitor row.

        Reads every field eagerly so the caller can build the request inside
        a database session and run the check itself outside of one.
        """
        return cls(
            target=monitor.target,
            timeout_seconds=monitor.timeout_seconds,
            expected_status=monitor.expected_status,
            expected_body_contains=monitor.expected_body_contains,
        )


@dataclass(frozen=True)
class CheckOutcome:
    """The result of running one check against a monitor's target."""

    success: bool
    latency_ms: Optional[float] = None
    status_code: Optional[int] = None
    error_message: Optional[str] = None


class Checker(Protocol):
    """Runs a single check against a target and reports the outcome."""

    async def check(self, request: CheckRequest) -> CheckOutcome: ...
