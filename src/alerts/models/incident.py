from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import Enum, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from alerts.models.base import Base

if TYPE_CHECKING:
    from alerts.models.monitor import Monitor


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class IncidentState(str, enum.Enum):
    OPEN = "open"
    RESOLVED = "resolved"


class Incident(Base):
    """A span of downtime/degradation for one monitor.

    Opened when a monitor crosses its failure threshold, closed when it
    crosses its recovery threshold. Notification sends are logged against
    the incident so we know what already went out and can dedup/escalate.
    """

    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    monitor_id: Mapped[int] = mapped_column(ForeignKey("monitors.id"), index=True)

    state: Mapped[IncidentState] = mapped_column(Enum(IncidentState), default=IncidentState.OPEN)
    cause: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(default=_utcnow)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    monitor: Mapped["Monitor"] = relationship(back_populates="incidents")
    notifications: Mapped[List["NotificationLog"]] = relationship(
        back_populates="incident", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"Incident(id={self.id}, monitor_id={self.monitor_id}, state={self.state.value})"


class NotificationLog(Base):
    """Record of one attempt to notify a channel about an incident."""

    __tablename__ = "notification_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id"), index=True)
    channel_name: Mapped[str] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(default=_utcnow)
    success: Mapped[bool] = mapped_column(default=False)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    incident: Mapped["Incident"] = relationship(back_populates="notifications")
