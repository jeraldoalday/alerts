from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import TYPE_CHECKING, List

from sqlalchemy import Enum, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from alerts.models.base import Base

if TYPE_CHECKING:
    from alerts.models.check_result import CheckResult
    from alerts.models.incident import Incident


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MonitorState(str, enum.Enum):
    UNKNOWN = "unknown"
    UP = "up"
    DEGRADED = "degraded"
    DOWN = "down"


class MonitorType(str, enum.Enum):
    HTTP = "http"
    TCP = "tcp"
    DNS = "dns"
    SSL = "ssl"


class Monitor(Base):
    """A single thing being watched (an HTTP endpoint, a TCP port, ...).

    The row mirrors a `MonitorConfig` entry from the YAML config, plus the
    mutable runtime state (current status, consecutive pass/fail streaks)
    that the scheduler updates after every check.
    """

    __tablename__ = "monitors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    type: Mapped[MonitorType] = mapped_column(Enum(MonitorType))
    target: Mapped[str] = mapped_column(String(500))

    interval_seconds: Mapped[int] = mapped_column(Integer, default=60)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=10)
    expected_status: Mapped[int] = mapped_column(Integer, default=200)
    failure_threshold: Mapped[int] = mapped_column(Integer, default=3)
    recovery_threshold: Mapped[int] = mapped_column(Integer, default=2)

    state: Mapped[MonitorState] = mapped_column(
        Enum(MonitorState), default=MonitorState.UNKNOWN
    )
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_successes: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)

    check_results: Mapped[List["CheckResult"]] = relationship(
        back_populates="monitor", cascade="all, delete-orphan"
    )
    incidents: Mapped[List["Incident"]] = relationship(
        back_populates="monitor", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"Monitor(id={self.id}, name={self.name!r}, state={self.state.value})"
