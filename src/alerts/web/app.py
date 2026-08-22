"""WSGI application serving the read-only monitor status dashboard.

Plain WSGI rather than a framework, to match the project's minimal
dependency footprint (SQLAlchemy/alembic/PyYAML/pydantic/httpx only). Routes
are dispatched by a simple prefix check in `__call__`; each handler opens its
own database session and returns before the response body is generated, so
nothing holds the session open across the WSGI response iterator.
"""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs

from alerts.db import Database
from alerts.models import IncidentState
from alerts.timeutil import as_utc_naive, utcnow
from alerts.web import metrics, queries, render
from alerts.web.stats import UptimeStats, compute_uptime_stats, summarize_states

StartResponse = Callable[[str, List[Tuple[str, str]]], Any]


def _incident_duration_seconds(incident, now) -> float:
    """How long an incident lasted, or has lasted so far.

    An open incident is measured against `now`, which is the only reading
    that keeps climbing while a service is down. Matches `IncidentRow` in the
    reporting module so the JSON feed and `alerts incidents` can't disagree
    about how long an outage has been running.
    """
    end = incident.resolved_at if incident.resolved_at is not None else now
    return (as_utc_naive(end) - as_utc_naive(incident.started_at)).total_seconds()


def _percentile_payload(stats: UptimeStats) -> Dict[str, Optional[float]]:
    """Latency percentiles as a JSON object keyed "p50", "p95", "p99".

    Nested under one key rather than flattened into three siblings so a
    client can iterate the percentiles it was given instead of hard-coding
    which three this version happens to report. Keys are the "pNN" spelling
    rather than bare numbers, because a JSON object key has to be a string
    and "95" invites a client to parse it back into one anyway.
    """
    return {f"p{percent}": value for percent, value in stats.latency_percentiles.items()}


def _decoded_path(environ: dict) -> str:
    """The request path as text, undoing WSGI's latin-1 transport encoding.

    PEP 3333 defines PATH_INFO as bytes smuggled through a `str` one
    character per byte (latin-1), and `wsgiref` decodes percent-escapes that
    way too. A monitor whose name isn't pure ASCII therefore arrives as
    mojibake and no lookup by name would ever match it; re-encoding to bytes
    and decoding as UTF-8 recovers the name the config declared.

    A path that doesn't fit that convention -- one an already-decoding server
    handed over, or plain garbage -- falls back to replacing whatever can't be
    encoded. That matters beyond tidiness: an undecodable byte arrives as a
    lone surrogate, and passing one through to a name lookup raises inside the
    database driver, turning a malformed URL into a 500 instead of a 404.
    """
    path = environ.get("PATH_INFO") or "/"
    try:
        return path.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return path.encode("utf-8", errors="replace").decode("utf-8")


class BadRequest(Exception):
    """A query parameter the caller has to fix, reported as a 400."""


def _int_param(
    params: Dict[str, List[str]], name: str, default: int, *, minimum: int, maximum: int
) -> int:
    """One integer query parameter, validated rather than coerced.

    Out-of-range and non-numeric values are refused instead of being clamped
    silently. A caller that asks for `limit=5000` and receives 200 rows has
    no way to tell that from a database with 200 incidents in it, and would
    reasonably conclude the other 4800 never happened.
    """
    raw = params.get(name)
    if not raw:
        return default
    try:
        value = int(raw[-1])
    except ValueError:
        raise BadRequest(f"{name} must be an integer") from None
    if not minimum <= value <= maximum:
        raise BadRequest(f"{name} must be between {minimum} and {maximum}")
    return value


def _flag_param(params: Dict[str, List[str]], name: str) -> bool:
    """A boolean query parameter, in the spellings a URL actually arrives in.

    Bare `?open` (which parses to an empty value) counts as true, as do the
    usual affirmative words. Anything else is false rather than an error:
    the parameter narrows a listing, so misreading one costs a caller some
    extra rows, not a wrong answer about whether anything is broken.
    """
    raw = params.get(name)
    if raw is None:
        return False
    return raw[-1].strip().lower() in ("", "1", "true", "yes", "on")


def _tag_param(params: Dict[str, List[str]], name: str = "tag") -> Optional[str]:
    """The tag to filter by, or None when the caller wants everything.

    A blank `?tag=` is treated as absent rather than as a tag nobody has:
    it is what a form submitted with the filter cleared sends, and answering
    that with an empty listing would be a strange way to say "all".
    """
    raw = params.get(name)
    if not raw:
        return None
    return raw[-1].strip() or None


class DashboardApp:
    def __init__(self, database: Database, recent_checks_limit: int = 50):
        self._database = database
        self._recent_checks_limit = recent_checks_limit

    def __call__(self, environ: dict, start_response: StartResponse) -> Iterable[bytes]:
        """Dispatch a request, answering HEAD as GET without the body.

        A HEAD response carries the status and headers a GET would, and no
        body -- so the route runs normally and only the body is dropped on
        the way out. Rejecting HEAD outright, as this used to, breaks the
        cheapest kind of uptime check there is, including the one this
        project's own HTTP monitors can now be configured to make against
        `/healthz`.
        """
        method = environ.get("REQUEST_METHOD", "GET")
        if method not in ("GET", "HEAD"):
            return self._respond(start_response, "405 Method Not Allowed", b"method not allowed")

        try:
            body = self._dispatch(
                _decoded_path(environ), environ.get("QUERY_STRING", ""), start_response
            )
        except BadRequest as exc:
            body = self._respond_json(start_response, "400 Bad Request", {"error": str(exc)})
        return [] if method == "HEAD" else body

    def _dispatch(
        self, path: str, query: str, start_response: StartResponse
    ) -> Iterable[bytes]:
        """Route an already-decoded path to a handler.

        `path` has been through percent-decoding exactly once, by the WSGI
        server, so the trailing segment of a `/monitors/<name>` route is the
        monitor name as configured. Decoding it a second time here -- as this
        used to -- reads the escapes in a name that legitimately contains a
        "%" as if they were the URL's own, so the dashboard's own link to a
        monitor called `cache%2Fhit` resolved to `cache/hit` and 404'd.
        """
        if path == "/":
            return self._index(start_response, query)
        if path == "/healthz":
            return self._healthz(start_response)
        if path == "/metrics":
            return self._metrics(start_response)
        if path == "/api/summary":
            return self._api_summary(start_response, query)
        if path == "/api/monitors":
            return self._api_monitors(start_response, query)
        if path == "/api/incidents":
            return self._api_incidents(start_response, query)
        if path.startswith("/api/monitors/"):
            return self._api_monitor_detail(start_response, path[len("/api/monitors/") :])
        if path.startswith("/monitors/"):
            return self._monitor_detail(start_response, path[len("/monitors/") :])
        return self._not_found(start_response)

    def _index(self, start_response: StartResponse, query: str = "") -> Iterable[bytes]:
        """The monitor table, optionally narrowed to one tag.

        The summary tiles are computed over the same filtered set, so a
        `?tag=prod` view counts what is down in `prod` rather than repeating
        the estate-wide numbers above a table that no longer matches them.

        The open-incident tile is the one exception: it is a count across
        every monitor, since narrowing it would need a join from incidents
        back to tags for a number whose whole job is being the one figure
        that doesn't depend on which view you are looking at.
        """
        tag = _tag_param(parse_qs(query, keep_blank_values=True))
        with self._database.session() as session:
            monitors = queries.list_monitors(session, tag=tag)
            rows = []
            for monitor in monitors:
                checks = queries.recent_check_results(
                    session, monitor.id, self._recent_checks_limit
                )
                stats = compute_uptime_stats(checks)
                last_checked_at = checks[0].checked_at if checks else None
                rows.append((monitor, stats, last_checked_at))
            summary = summarize_states(monitors, queries.count_open_incidents(session))
            body = render.render_index(
                rows, summary, tags=queries.known_tags(session), active_tag=tag
            )
        return self._respond_html(start_response, "200 OK", body)

    def _monitor_detail(self, start_response: StartResponse, name: str) -> Iterable[bytes]:
        with self._database.session() as session:
            monitor = queries.get_monitor_by_name(session, name)
            if monitor is None:
                body = render.render_not_found(f"monitor {name!r} not found")
                return self._respond_html(start_response, "404 Not Found", body)

            checks = queries.recent_check_results(session, monitor.id, self._recent_checks_limit)
            incidents = queries.recent_incidents(session, monitor.id)
            stats = compute_uptime_stats(checks)
            body = render.render_monitor_detail(monitor, stats, checks, incidents)
        return self._respond_html(start_response, "200 OK", body)

    def _api_monitor_detail(self, start_response: StartResponse, name: str) -> Iterable[bytes]:
        with self._database.session() as session:
            monitor = queries.get_monitor_by_name(session, name)
            if monitor is None:
                error_payload = {"error": f"monitor {name!r} not found"}
                return self._respond_json(start_response, "404 Not Found", error_payload)

            checks = queries.recent_check_results(session, monitor.id, self._recent_checks_limit)
            incidents = queries.recent_incidents(session, monitor.id)
            stats = compute_uptime_stats(checks)
            payload: Dict[str, Any] = {
                "name": monitor.name,
                "type": monitor.type.value,
                "target": monitor.target,
                "tags": list(monitor.tags or []),
                "state": monitor.state.value,
                "paused": monitor.is_paused,
                "degraded_latency_ms": monitor.degraded_latency_ms,
                "uptime_percentage": stats.uptime_percentage,
                "average_latency_ms": stats.average_latency_ms,
                "latency_percentiles": _percentile_payload(stats),
                "recent_checks": [
                    {
                        "checked_at": check.checked_at.isoformat(),
                        "success": check.success,
                        "latency_ms": check.latency_ms,
                        "status_code": check.status_code,
                        "error_message": check.error_message,
                    }
                    for check in checks
                ],
                "incidents": [
                    {
                        "state": incident.state.value,
                        "started_at": incident.started_at.isoformat(),
                        "resolved_at": (
                            incident.resolved_at.isoformat() if incident.resolved_at else None
                        ),
                        "cause": incident.cause,
                    }
                    for incident in incidents
                ],
            }
        return self._respond_json(start_response, "200 OK", payload)

    def _api_monitors(self, start_response: StartResponse, query: str = "") -> Iterable[bytes]:
        """Every monitor's current reading, optionally narrowed to one tag.

        The response echoes the tag it filtered on rather than leaving the
        caller to remember what it asked for, which matters for a client that
        polls several tags into one view.
        """
        tag = _tag_param(parse_qs(query, keep_blank_values=True))
        with self._database.session() as session:
            payload = []
            for monitor in queries.list_monitors(session, tag=tag):
                checks = queries.recent_check_results(
                    session, monitor.id, self._recent_checks_limit
                )
                stats = compute_uptime_stats(checks)
                payload.append(
                    {
                        "name": monitor.name,
                        "type": monitor.type.value,
                        "tags": list(monitor.tags or []),
                        "state": monitor.state.value,
                        "paused": monitor.is_paused,
                        "degraded_latency_ms": monitor.degraded_latency_ms,
                        "uptime_percentage": stats.uptime_percentage,
                        "average_latency_ms": stats.average_latency_ms,
                        "latency_percentiles": _percentile_payload(stats),
                    }
                )
        return self._respond_json(
            start_response, "200 OK", {"monitors": payload, "tag": tag}
        )

    def _api_incidents(self, start_response: StartResponse, query: str) -> Iterable[bytes]:
        """The estate-wide incident feed, newest first.

        The per-monitor history is already on /api/monitors/<name>; this is
        the cross-service view a status page or a weekly report is built
        from, so it takes filters rather than being one fixed listing:
        `?monitor=`, `?open`, `?days=`, `?limit=`.

        Each incident carries a server-computed `duration_seconds` -- measured
        against now while it is still open -- so every consumer doesn't
        reimplement that arithmetic, and none of them has to guess what the
        server's clock says.
        """
        params = parse_qs(query, keep_blank_values=True)
        limit = _int_param(params, "limit", 50, minimum=1, maximum=500)
        days = _int_param(params, "days", 7, minimum=0, maximum=3650)
        open_only = _flag_param(params, "open")
        monitor_name = params.get("monitor", [None])[-1] or None

        now = utcnow()
        since = as_utc_naive(now) - timedelta(days=days) if days > 0 else None

        with self._database.session() as session:
            monitor_id: Optional[int] = None
            if monitor_name is not None:
                monitor = queries.get_monitor_by_name(session, monitor_name)
                if monitor is None:
                    return self._respond_json(
                        start_response,
                        "404 Not Found",
                        {"error": f"monitor {monitor_name!r} not found"},
                    )
                monitor_id = monitor.id

            names = {m.id: m.name for m in queries.list_monitors(session)}
            incidents = queries.list_incidents(
                session,
                monitor_id=monitor_id,
                since=since,
                open_only=open_only,
                limit=limit,
            )
            payload = [
                {
                    "monitor": names.get(incident.monitor_id),
                    "state": incident.state.value,
                    "started_at": incident.started_at.isoformat(),
                    "resolved_at": (
                        incident.resolved_at.isoformat() if incident.resolved_at else None
                    ),
                    "duration_seconds": _incident_duration_seconds(incident, now),
                    "cause": incident.cause,
                }
                for incident in incidents
            ]
        open_count = sum(1 for row in payload if row["state"] == IncidentState.OPEN.value)
        return self._respond_json(
            start_response, "200 OK", {"incidents": payload, "open": open_count}
        )

    def _api_summary(self, start_response: StartResponse, query: str = "") -> Iterable[bytes]:
        """Aggregate state counts, for a wallboard or an external alert check.

        Separate from /api/monitors because it stays a constant-size response
        however many monitors exist, and answers the one question a polling
        client usually has: is anything broken right now?

        `?tag=` narrows it to one group, which is what makes it usable as a
        per-team check: a payments wallboard asking "is anything of ours
        down" shouldn't turn red for a monitor nobody on that team owns.
        `open_incidents` stays estate-wide -- see `_index`.
        """
        tag = _tag_param(parse_qs(query, keep_blank_values=True))
        with self._database.session() as session:
            summary = summarize_states(
                queries.list_monitors(session, tag=tag),
                queries.count_open_incidents(session),
            )
        payload = {
            "total": summary.total,
            "open_incidents": summary.open_incidents,
            "paused": summary.paused,
            "tag": tag,
            "states": {state.value: count for state, count in summary.counts.items()},
        }
        return self._respond_json(start_response, "200 OK", payload)

    def _metrics(self, start_response: StartResponse) -> Iterable[bytes]:
        """Prometheus exposition of every monitor's current reading.

        Built from the same queries and aggregation the index page uses, so
        a scraped series and the dashboard can never disagree about what a
        monitor's uptime is.
        """
        with self._database.session() as session:
            rows = []
            for monitor in queries.list_monitors(session):
                checks = queries.recent_check_results(
                    session, monitor.id, self._recent_checks_limit
                )
                rows.append((monitor, compute_uptime_stats(checks)))
            body = metrics.render_metrics(rows, queries.count_open_incidents(session))
        return self._respond(
            start_response, "200 OK", body.encode("utf-8"), content_type=metrics.CONTENT_TYPE
        )

    def _healthz(self, start_response: StartResponse) -> Iterable[bytes]:
        return self._respond_json(start_response, "200 OK", {"status": "ok"})

    def _not_found(self, start_response: StartResponse) -> Iterable[bytes]:
        return self._respond_html(start_response, "404 Not Found", render.render_not_found())

    def _respond_html(self, start_response: StartResponse, status: str, body: str) -> Iterable[bytes]:
        return self._respond(
            start_response, status, body.encode("utf-8"), content_type="text/html; charset=utf-8"
        )

    def _respond_json(self, start_response: StartResponse, status: str, payload: dict) -> Iterable[bytes]:
        return self._respond(
            start_response,
            status,
            json.dumps(payload).encode("utf-8"),
            content_type="application/json",
        )

    @staticmethod
    def _respond(
        start_response: StartResponse,
        status: str,
        body: bytes,
        content_type: str = "text/plain; charset=utf-8",
    ) -> Iterable[bytes]:
        headers = [("Content-Type", content_type), ("Content-Length", str(len(body)))]
        start_response(status, headers)
        return [body]
