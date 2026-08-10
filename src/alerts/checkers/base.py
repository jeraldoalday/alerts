"""Shared types for check implementations."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass(frozen=True)
class CheckOutcome:
    """The result of running one check against a monitor's target."""

    success: bool
    latency_ms: Optional[float] = None
    status_code: Optional[int] = None
    error_message: Optional[str] = None


class Checker(Protocol):
    """Runs a single check against a target and reports the outcome."""

    async def check(
        self, *, target: str, timeout_seconds: float, expected_status: int
    ) -> CheckOutcome: ...
