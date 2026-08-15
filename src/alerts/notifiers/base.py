"""Shared types for channel notifiers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass(frozen=True)
class NotifyResult:
    """The result of attempting to send one notification to a channel."""

    success: bool
    error_message: Optional[str] = None


class Notifier(Protocol):
    """Sends a single rendered message to one configured channel."""

    async def send(self, message: str) -> NotifyResult: ...
