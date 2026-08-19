"""Decides which channels need notifying about an incident.

Sits between incident lifecycle (`incidents.sync_incident`) and the
per-channel notifiers that will actually send. It owns three concerns:

- dedup: a channel already successfully notified of a given
  `NotificationKind` for an incident isn't notified of it again
- cooldown: while an incident stays open, a channel already notified isn't
  re-notified until `cooldown_seconds` has elapsed since its last
  successful notification
- escalation: once the cooldown has elapsed on an still-open incident,
  that channel gets an ESCALATION decision instead of a repeated OPENED,
  and escalations keep repeating on the same cooldown for as long as the
  incident stays open

Only successful sends count for dedup/cooldown purposes -- a failed
attempt should be retried, not silently dropped.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, List

from alerts.models import Incident, IncidentState, NotificationKind
from alerts.timeutil import as_utc_naive


@dataclass(frozen=True)
class NotificationDecision:
    """One channel that needs notifying, and what it should be told."""

    channel_name: str
    kind: NotificationKind


def decide_notifications(
    incident: Incident,
    channel_names: Iterable[str],
    cooldown_seconds: int,
    *,
    now: datetime,
) -> List[NotificationDecision]:
    decisions: List[NotificationDecision] = []
    for channel_name in channel_names:
        sent = [
            n for n in incident.notifications if n.channel_name == channel_name and n.success
        ]

        if incident.state == IncidentState.RESOLVED:
            if not any(n.kind == NotificationKind.RESOLVED for n in sent):
                decisions.append(NotificationDecision(channel_name, NotificationKind.RESOLVED))
            continue

        opened = [n for n in sent if n.kind == NotificationKind.OPENED]
        if not opened:
            decisions.append(NotificationDecision(channel_name, NotificationKind.OPENED))
            continue

        last_sent_at = max(n.sent_at for n in sent)
        if as_utc_naive(now) - as_utc_naive(last_sent_at) >= timedelta(seconds=cooldown_seconds):
            decisions.append(NotificationDecision(channel_name, NotificationKind.ESCALATION))

    return decisions
