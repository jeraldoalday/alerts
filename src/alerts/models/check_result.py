from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, Float, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from alerts.models.base import Base
from alerts.timeutil import utcnow

if TYPE_CHECKING:
    from alerts.models.monitor import Monitor


class CheckResult(Base):
    """One executed check against a monitor's target.

    A row is written every time the scheduler runs a check, regardless of
    outcome, so the raw history can drive uptime percentages and latency
    charts independent of the derived monitor state machine.
    """

    __tablename__ = "check_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    monitor_id: Mapped[int] = mapped_column(ForeignKey("monitors.id"), index=True)

    checked_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    success: Mapped[bool] = mapped_column(Boolean)
    latency_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    monitor: Mapped["Monitor"] = relationship(back_populates="check_results")

    def __repr__(self) -> str:
        outcome = "ok" if self.success else "fail"
        return f"CheckResult(monitor_id={self.monitor_id}, {outcome}, at={self.checked_at.isoformat()})"
