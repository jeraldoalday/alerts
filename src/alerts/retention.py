"""Deletes history that has aged out of the configured retention window.

The scheduler writes one `check_results` row per monitor per interval and
never removes any, so a handful of monitors on short intervals fills a
database indefinitely -- a monitor checked every 15 seconds is 5,760 rows a
day on its own. This module implements the policy in `RetentionConfig`;
`alerts prune` is the entrypoint that applies it.

Two deletion strategies are used deliberately:

- check results are removed with a bulk `DELETE`, since nothing references
  them and there are far more of them than of anything else
- incidents are removed one ORM object at a time, so the `delete-orphan`
  cascade on `Incident.notifications` takes their notification log rows with
  them. A bulk delete would bypass the ORM cascade and strand those rows
  pointing at an incident id that no longer exists.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy.orm import Session

from alerts.config import RetentionConfig
from alerts.models import CheckResult, Incident, IncidentState
from alerts.timeutil import as_utc_naive, utcnow


@dataclass(frozen=True)
class PruneSummary:
    """How many rows a prune removed (or would remove, for a dry run)."""

    check_results_deleted: int = 0
    incidents_deleted: int = 0
    notification_logs_deleted: int = 0

    @property
    def total_deleted(self) -> int:
        return (
            self.check_results_deleted
            + self.incidents_deleted
            + self.notification_logs_deleted
        )

    def describe(self) -> str:
        return (
            f"{self.check_results_deleted} check result(s), "
            f"{self.incidents_deleted} resolved incident(s), "
            f"{self.notification_logs_deleted} notification log(s)"
        )


def _cutoff(now: datetime, older_than_days: int) -> datetime:
    """The naive-UTC timestamp rows must predate to be pruned.

    Naive on purpose: the stored timestamps come back from SQLite without
    tzinfo, and binding an aware value against them compares ISO strings that
    carry a "+00:00" suffix with ones that don't.
    """
    return as_utc_naive(now) - timedelta(days=older_than_days)


def _monitor_ids_with_check_results(session: Session) -> List[int]:
    """Distinct monitor ids appearing in check_results.

    Read off the check results themselves rather than the monitors table so
    rows left behind by a monitor that's gone are still pruned.
    """
    return [row[0] for row in session.query(CheckResult.monitor_id).distinct()]


def _protected_check_result_ids(
    session: Session, monitor_id: int, min_per_monitor: int
) -> List[int]:
    """Ids of the newest `min_per_monitor` checks for one monitor."""
    if min_per_monitor <= 0:
        return []
    rows = (
        session.query(CheckResult.id)
        .filter(CheckResult.monitor_id == monitor_id)
        .order_by(CheckResult.checked_at.desc(), CheckResult.id.desc())
        .limit(min_per_monitor)
        .all()
    )
    return [row[0] for row in rows]


def prune_check_results(
    session: Session,
    *,
    older_than_days: Optional[int],
    min_per_monitor: int = 0,
    now: Optional[datetime] = None,
    dry_run: bool = False,
) -> int:
    """Delete check results older than the cutoff, keeping a per-monitor floor.

    Returns the number of rows deleted, or the number that would have been
    for a dry run. A `None` age keeps everything.
    """
    if older_than_days is None:
        return 0

    cutoff = _cutoff(now or utcnow(), older_than_days)
    deleted = 0
    for monitor_id in _monitor_ids_with_check_results(session):
        query = session.query(CheckResult).filter(
            CheckResult.monitor_id == monitor_id, CheckResult.checked_at < cutoff
        )
        protected = _protected_check_result_ids(session, monitor_id, min_per_monitor)
        if protected:
            query = query.filter(CheckResult.id.notin_(protected))

        if dry_run:
            deleted += query.count()
        else:
            deleted += query.delete(synchronize_session=False)
    return deleted


def prune_resolved_incidents(
    session: Session,
    *,
    older_than_days: Optional[int],
    now: Optional[datetime] = None,
    dry_run: bool = False,
) -> PruneSummary:
    """Delete resolved incidents (and their notification logs) past the cutoff.

    Open incidents are never pruned, however old: an incident that's still
    open is current state, not history.
    """
    if older_than_days is None:
        return PruneSummary()

    cutoff = _cutoff(now or utcnow(), older_than_days)
    incidents = (
        session.query(Incident)
        .filter(
            Incident.state == IncidentState.RESOLVED,
            Incident.resolved_at.isnot(None),
            Incident.resolved_at < cutoff,
        )
        .all()
    )

    notification_logs = sum(len(incident.notifications) for incident in incidents)
    if not dry_run:
        for incident in incidents:
            session.delete(incident)

    return PruneSummary(
        incidents_deleted=len(incidents), notification_logs_deleted=notification_logs
    )


def prune(
    session: Session,
    retention: RetentionConfig,
    *,
    now: Optional[datetime] = None,
    dry_run: bool = False,
) -> PruneSummary:
    """Apply the whole retention policy in one pass."""
    now = now or utcnow()
    check_results_deleted = prune_check_results(
        session,
        older_than_days=retention.check_results_days,
        min_per_monitor=retention.min_check_results_per_monitor,
        now=now,
        dry_run=dry_run,
    )
    incidents = prune_resolved_incidents(
        session,
        older_than_days=retention.resolved_incidents_days,
        now=now,
        dry_run=dry_run,
    )
    return PruneSummary(
        check_results_deleted=check_results_deleted,
        incidents_deleted=incidents.incidents_deleted,
        notification_logs_deleted=incidents.notification_logs_deleted,
    )
