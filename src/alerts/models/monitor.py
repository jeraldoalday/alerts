from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence

from sqlalchemy import JSON, Boolean, Enum, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from alerts.dependencies import is_held_back
from alerts.models.base import Base
from alerts.slo import DEFAULT_WINDOW_DAYS as DEFAULT_SLO_WINDOW_DAYS
from alerts.tags import normalise as normalise_tag
from alerts.timeutil import utcnow

if TYPE_CHECKING:
    from alerts.models.check_result import CheckResult
    from alerts.models.incident import Incident


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
    # Successful checks at or above this latency mark the monitor DEGRADED
    # rather than UP. NULL means the monitor has no latency budget at all.
    degraded_latency_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # HTTP only: substring the response body must contain. NULL means the
    # status code is the only assertion.
    expected_body_contains: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # HTTP only: substring the response body must NOT contain. NULL means
    # nothing is being watched for. Checked independently of
    # expected_body_contains, not as its negation.
    expected_body_not_contains: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # HTTP only: the request method, and the headers sent with every check.
    # Header values are stored exactly as configured, `${VAR}` placeholders
    # included -- they are expanded per check, so a resolved API token lives
    # in the checking process and never in this table.
    method: Mapped[str] = mapped_column(String(10), default="GET", server_default="GET")
    headers: Mapped[Dict[str, str]] = mapped_column(JSON, default=dict)
    channels: Mapped[List[str]] = mapped_column(JSON, default=list)
    # The availability this monitor is expected to hold, as a percentage,
    # over a rolling window of `slo_window_days`. NULL means no objective:
    # the monitor is watched, but nothing is being promised about it. The
    # window is NOT NULL and carries a default because it is only ever read
    # alongside a target, and a target with no window would have nothing to
    # be measured over.
    slo_target: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    slo_window_days: Mapped[int] = mapped_column(
        Integer, default=DEFAULT_SLO_WINDOW_DAYS, server_default=str(DEFAULT_SLO_WINDOW_DAYS)
    )
    # Free-form grouping labels, normalised by the config loader before they
    # get here. Stored as JSON alongside `channels` rather than in a join
    # table: they carry no behaviour and nothing joins on them, and a
    # handful of strings per monitor is not a relation worth modelling.
    tags: Mapped[List[str]] = mapped_column(JSON, default=list)
    # The monitors this one needs in order to be healthy, by name. Stored
    # as JSON alongside `tags` rather than as a self-referential foreign
    # key: the config is the source of truth for the edges, and a real
    # relation would have to be resolved (and could dangle) every time a
    # monitor is added, renamed, or removed from the file. The names are
    # validated against the configured set at load time, which is where a
    # typo can still be reported as one.
    depends_on: Mapped[List[str]] = mapped_column(JSON, default=list, server_default="[]")
    # False once a monitor disappears from the config. The row (and its
    # history) is kept, but the scheduler stops checking it -- see
    # `sync_monitors`.
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")

    state: Mapped[MonitorState] = mapped_column(
        Enum(MonitorState), default=MonitorState.UNKNOWN
    )
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_successes: Mapped[int] = mapped_column(Integer, default=0)
    # Successful checks in a row that came back *within* the latency budget.
    # Separate from consecutive_successes because leaving DEGRADED is a
    # question about latency, not about the check passing at all: a monitor
    # that has been slow for hours has a long success streak and would
    # otherwise jump back to UP on the first fast check it ever gets.
    consecutive_fast_checks: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )

    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    check_results: Mapped[List["CheckResult"]] = relationship(
        back_populates="monitor", cascade="all, delete-orphan"
    )
    incidents: Mapped[List["Incident"]] = relationship(
        back_populates="monitor", cascade="all, delete-orphan"
    )

    def has_tag(self, tag: str) -> bool:
        """Whether this monitor carries a tag, compared on the folded spelling.

        The stored tags are already normalised, so only the argument needs
        folding -- which is what makes a `?tag=Prod` in a URL or a `--tag
        Prod` at a terminal match what the config declared.
        """
        return normalise_tag(tag) in (self.tags or [])

    @property
    def has_slo(self) -> bool:
        """Whether an availability objective has been declared for this monitor.

        Reported everywhere rather than inferred from a target of 0, because
        0 is not a valid target -- a service allowed to be down always has no
        objective, and NULL is how that is spelled.
        """
        return self.slo_target is not None

    @property
    def blocks_dependents(self) -> bool:
        """Whether this monitor being down should hold back its dependents' alerts.

        The one definition of what counts as a down upstream, because three
        surfaces ask the question -- the scheduler deciding whether to page,
        the dashboard explaining why a service is quiet, and `alerts deps`
        -- and an answer that differed between them would mean the page and
        the page explaining the page disagreeing.

        A paused monitor never blocks anything, however bad its last reading
        was. Its state is frozen at the moment the config stopped declaring
        it, and suppressing a live service's alerts on a reading nobody is
        updating would suppress them forever.
        """
        return self.state == MonitorState.DOWN and not self.is_paused

    def alerts_held_back_by(self, blocked_by: Sequence[str]) -> bool:
        """Whether this monitor is failing and being kept quiet by an upstream.

        The two halves are genuinely separate questions and both surfaces
        that render this got them confused at least once: a healthy service
        sitting above a broken database has a down upstream and nothing to
        page about, so nothing of its is being held back. Only a monitor
        that is down itself -- and live, so there is a check still running
        to fail -- has an alert that isn't going out.

        Takes the upstreams rather than looking them up, because the caller
        already has them: the page, the JSON payload, and the exposition all
        walk a set of monitors against one graph.
        """
        return is_held_back(
            is_down=self.state == MonitorState.DOWN,
            is_paused=self.is_paused,
            blocked_by=blocked_by,
        )

    @property
    def is_paused(self) -> bool:
        """Whether the config has stopped declaring this monitor.

        Compares against False rather than testing falsiness so a Monitor
        built in memory -- one whose column defaults a flush hasn't applied
        yet -- reads as active, which is what it becomes once persisted. The
        column is NOT NULL, so anything loaded from the database answers
        truthfully either way.
        """
        return self.enabled is False

    def __repr__(self) -> str:
        return f"Monitor(id={self.id}, name={self.name!r}, state={self.state.value})"
