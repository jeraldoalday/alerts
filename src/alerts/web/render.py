"""HTML rendering for the dashboard.

Deliberately dependency-free: no template engine, just `str` building with
`html.escape` on every piece of data that ultimately comes from a monitor
config or a checker's error message (both operator-controlled, but never
trusted as safe-to-embed markup).
"""
from __future__ import annotations

from html import escape
from typing import List, Optional, Sequence, Tuple
from urllib.parse import quote

from alerts.models import CheckResult, Incident, IncidentState, Monitor, MonitorState
from alerts.web.stats import StateSummary, UptimeStats

_STATE_COLORS = {
    MonitorState.UP: "#1a7f37",
    MonitorState.DEGRADED: "#9a6700",
    MonitorState.DOWN: "#cf222e",
    MonitorState.UNKNOWN: "#57606a",
}

_STYLE = """
body { font-family: -apple-system, system-ui, sans-serif; margin: 2rem; color: #1f2328; }
h1 { margin-bottom: 0.25rem; }
a { color: #0969da; text-decoration: none; }
a:hover { text-decoration: underline; }
table { border-collapse: collapse; width: 100%; margin-top: 1rem; }
th, td { text-align: left; padding: 0.5rem 0.75rem; border-bottom: 1px solid #d0d7de; }
th { color: #57606a; font-weight: 600; font-size: 0.85rem; text-transform: uppercase; }
.badge { display: inline-block; padding: 0.15rem 0.6rem; border-radius: 999px; color: #fff;
  font-size: 0.8rem; font-weight: 600; }
.muted { color: #57606a; }
.nav { margin-bottom: 1rem; }
.summary { display: flex; gap: 0.75rem; flex-wrap: wrap; margin: 1rem 0 0; padding: 0;
  list-style: none; }
.tile { border: 1px solid #d0d7de; border-radius: 6px; padding: 0.5rem 0.9rem; min-width: 4.5rem; }
.tile .count { display: block; font-size: 1.4rem; font-weight: 600; line-height: 1.2; }
.tile .label { font-size: 0.75rem; text-transform: uppercase; color: #57606a; letter-spacing: 0.03em; }
.tags { margin: 1rem 0 0; }
.tag { display: inline-block; padding: 0.1rem 0.5rem; margin-right: 0.3rem; border-radius: 4px;
  background: #eaeef2; color: #57606a; font-size: 0.78rem; text-decoration: none; }
.tag:hover { background: #d0d7de; text-decoration: none; }
.tag.current { background: #0969da; color: #fff; }
"""


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<title>{escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body>{body}</body></html>"
    )


def _url_query(value: str) -> str:
    """Percent-encode a value for use in a query string.

    Separate from `_url_path` because the two escape different things: a
    space is legal in a path segment's encoding as `%20` either way, but the
    `&` and `=` that delimit query parameters must not survive into one.
    """
    return escape(quote(value, safe=""))


def _url_path(monitor_name: str) -> str:
    """Percent-encode a monitor name for use in a URL path.

    Names may hold anything but spaces and slashes, so an unencoded one can
    carry a "#" or "?" that truncates the path into a fragment or a query, or
    non-ASCII characters the browser percent-encodes itself into something
    the route then fails to match.
    """
    return escape(quote(monitor_name, safe=""))


def _tag_class(tag: Optional[str], active: Optional[str]) -> str:
    """The CSS classes for one tag link, marking the one being filtered on."""
    return "tag current" if tag == active else "tag"


def _tag_links(monitor: Monitor, active: Optional[str] = None) -> str:
    """A monitor's tags, each linking to the index filtered by it.

    Links rather than plain text: seeing that a monitor is tagged `payments`
    and immediately wanting the rest of `payments` is the whole reason the
    tag is on the page. The one currently being filtered on is marked so the
    view says what it is showing, since a filtered index otherwise looks
    exactly like an estate that has lost most of its monitors.
    """
    if not monitor.tags:
        return ""
    return " ".join(
        f'<a class="{_tag_class(tag, active)}" href="/?tag={_url_query(tag)}">{escape(tag)}</a>'
        for tag in monitor.tags
    )


def render_tag_filter(tags: Sequence[str], active: Optional[str] = None) -> str:
    """The row of tag filters above the index, with the active one marked.

    Rendered only when a tag exists to filter by, so an install that doesn't
    use tags never sees the control at all.
    """
    if not tags:
        return ""
    links = [f'<a class="{_tag_class(None, active)}" href="/">all</a>']
    links += [
        f'<a class="{_tag_class(tag, active)}" href="/?tag={_url_query(tag)}">{escape(tag)}</a>'
        for tag in tags
    ]
    return f'<p class="tags">{" ".join(links)}</p>'


def _paused_marker(monitor: Monitor) -> str:
    """Marks a monitor the config no longer declares.

    Its state, uptime, and last-checked column are all frozen at the moment
    checking stopped, so without saying so the row reads as live data that
    happens to have gone stale.
    """
    return '<span class="muted"> &middot; paused</span>' if monitor.is_paused else ""


def _state_badge(state: MonitorState) -> str:
    color = _STATE_COLORS.get(state, _STATE_COLORS[MonitorState.UNKNOWN])
    return f'<span class="badge" style="background:{color}">{escape(state.value)}</span>'


def _format_percentage(value: Optional[float]) -> str:
    return f"{value:.1f}%" if value is not None else "&mdash;"


def _format_latency(value: Optional[float]) -> str:
    return f"{value:.0f} ms" if value is not None else "&mdash;"


def _format_timestamp(value) -> str:
    return escape(value.isoformat(timespec="seconds")) if value is not None else "&mdash;"


def _percentile_summary(stats: UptimeStats) -> str:
    """The latency percentiles as one muted line of "p50 12 ms" pairs.

    Rendered next to the mean rather than instead of it: the mean is the
    number people quote, the percentiles are the ones that explain why it
    doesn't match what anyone experienced.

    A window with no recorded latency at all renders nothing, rather than
    three em dashes that take up a line to say the same absence the mean
    beside them already shows.
    """
    percentiles = stats.latency_percentiles
    if all(value is None for value in percentiles.values()):
        return ""
    pairs = " &nbsp; ".join(
        f"p{percent} {_format_latency(value)}" for percent, value in percentiles.items()
    )
    return f'<span class="muted">{pairs}</span>'


def _summary_tile(label: str, count: int, color: Optional[str] = None) -> str:
    style = f' style="color:{color}"' if color and count else ""
    return (
        '<li class="tile">'
        f'<span class="count"{style}>{count}</span>'
        f'<span class="label">{escape(label)}</span>'
        "</li>"
    )


def render_summary(summary: StateSummary) -> str:
    """A row of at-a-glance tiles: one per monitor state, plus open incidents.

    Rendered even when every monitor is up -- an operator opening the
    dashboard mid-incident shouldn't have to count table rows to find out how
    much is broken, and "0 down" is itself the reassurance they came for.
    """
    tiles = [_summary_tile("monitors", summary.total)]
    tiles += [
        _summary_tile(state.value, summary.counts[state], _STATE_COLORS[state])
        for state in (MonitorState.UP, MonitorState.DEGRADED, MonitorState.DOWN)
        if summary.counts[state] or state != MonitorState.DEGRADED
    ]
    if summary.counts[MonitorState.UNKNOWN]:
        tiles.append(
            _summary_tile("unknown", summary.counts[MonitorState.UNKNOWN], _STATE_COLORS[MonitorState.UNKNOWN])
        )
    tiles.append(
        _summary_tile("open incidents", summary.open_incidents, _STATE_COLORS[MonitorState.DOWN])
    )
    if summary.paused:
        tiles.append(_summary_tile("paused", summary.paused))
    return f'<ul class="summary">{"".join(tiles)}</ul>'


def render_index(
    rows: Sequence[Tuple[Monitor, UptimeStats, object]],
    summary: Optional[StateSummary] = None,
    tags: Sequence[str] = (),
    active_tag: Optional[str] = None,
) -> str:
    body_rows: List[str] = []
    for monitor, stats, last_checked_at in rows:
        body_rows.append(
            "<tr>"
            f'<td><a href="/monitors/{_url_path(monitor.name)}">{escape(monitor.name)}</a>'
            f"{_paused_marker(monitor)}</td>"
            f"<td>{escape(monitor.type.value)}</td>"
            f"<td>{_tag_links(monitor, active_tag)}</td>"
            f"<td>{_state_badge(monitor.state)}</td>"
            f"<td>{_format_percentage(stats.uptime_percentage)}</td>"
            f"<td>{_format_latency(stats.average_latency_ms)}</td>"
            f"<td>{_format_latency(stats.p95_latency_ms)}</td>"
            f"<td>{_format_timestamp(last_checked_at)}</td>"
            "</tr>"
        )

    # The empty message depends on why the table is empty. A filtered view
    # showing nothing is not an install with no monitors, and saying so would
    # send someone looking for a configuration problem that isn't there.
    empty_message = (
        f"No monitors tagged {escape(active_tag)}."
        if active_tag is not None
        else "No monitors configured."
    )
    empty_row = f'<tr><td colspan="8" class="muted">{empty_message}</td></tr>'
    table = (
        "<table><thead><tr><th>Monitor</th><th>Type</th><th>Tags</th><th>State</th>"
        "<th>Uptime</th><th>Avg latency</th><th>p95 latency</th>"
        "<th>Last checked</th></tr></thead>"
        f"<tbody>{''.join(body_rows) or empty_row}</tbody>"
        "</table>"
    )
    summary_html = render_summary(summary) if summary is not None else ""
    filter_html = render_tag_filter(tags, active_tag)
    heading = "Monitors" if active_tag is None else f"Monitors: {escape(active_tag)}"
    return _page(
        "alerts dashboard", f"<h1>{heading}</h1>{summary_html}{filter_html}{table}"
    )


def _check_result_row(result: CheckResult) -> str:
    outcome = '<span class="badge" style="background:#1a7f37">ok</span>' if result.success else (
        '<span class="badge" style="background:#cf222e">fail</span>'
    )
    detail = escape(result.error_message) if result.error_message else "&mdash;"
    return (
        "<tr>"
        f"<td>{_format_timestamp(result.checked_at)}</td>"
        f"<td>{outcome}</td>"
        f"<td>{_format_latency(result.latency_ms)}</td>"
        f"<td>{result.status_code if result.status_code is not None else '&mdash;'}</td>"
        f"<td>{detail}</td>"
        "</tr>"
    )


def _incident_row(incident: Incident) -> str:
    state_color = "#cf222e" if incident.state == IncidentState.OPEN else "#1a7f37"
    return (
        "<tr>"
        f'<td><span class="badge" style="background:{state_color}">{escape(incident.state.value)}</span></td>'
        f"<td>{_format_timestamp(incident.started_at)}</td>"
        f"<td>{_format_timestamp(incident.resolved_at)}</td>"
        f"<td>{escape(incident.cause) if incident.cause else '&mdash;'}</td>"
        "</tr>"
    )


def _latency_budget_note(monitor: Monitor) -> str:
    """Explains a DEGRADED badge by naming the budget the monitor is missing.

    Without it the detail page shows a monitor as degraded while every check
    in the table below reads "ok", which looks like a bug rather than a
    latency budget doing its job.
    """
    if monitor.degraded_latency_ms is None:
        return ""
    return (
        f'<span class="muted">&nbsp; degraded above '
        f"{_format_latency(monitor.degraded_latency_ms)}</span>"
    )


def render_monitor_detail(
    monitor: Monitor,
    stats: UptimeStats,
    check_results: Sequence[CheckResult],
    incidents: Sequence[Incident],
) -> str:
    empty_checks_row = '<tr><td colspan="5" class="muted">No checks yet.</td></tr>'
    checks_table = (
        "<table><thead><tr><th>Checked at</th><th>Result</th><th>Latency</th>"
        "<th>Status</th><th>Error</th></tr></thead>"
        f"<tbody>{''.join(_check_result_row(r) for r in check_results) or empty_checks_row}</tbody>"
        "</table>"
    )
    empty_incidents_row = '<tr><td colspan="4" class="muted">No incidents.</td></tr>'
    incidents_table = (
        "<table><thead><tr><th>State</th><th>Started</th><th>Resolved</th>"
        "<th>Cause</th></tr></thead>"
        f"<tbody>{''.join(_incident_row(i) for i in incidents) or empty_incidents_row}</tbody>"
        "</table>"
    )

    paused_note = (
        '<p class="muted">Paused: this monitor is no longer in the config, so nothing '
        "below is being updated.</p>"
        if monitor.is_paused
        else ""
    )
    body = (
        '<div class="nav"><a href="/">&larr; all monitors</a></div>'
        f"<h1>{escape(monitor.name)}</h1>"
        f'<p class="muted">{escape(monitor.type.value)} &middot; {escape(monitor.target)}</p>'
        f'{f"<p>{_tag_links(monitor)}</p>" if monitor.tags else ""}'
        f"{paused_note}"
        f"<p>{_state_badge(monitor.state)} "
        f"&nbsp; uptime {_format_percentage(stats.uptime_percentage)} "
        f"&nbsp; avg latency {_format_latency(stats.average_latency_ms)}"
        f"{_latency_budget_note(monitor)}</p>"
        f"<p>{_percentile_summary(stats)}</p>"
        f"<h2>Recent checks</h2>{checks_table}"
        f"<h2>Incidents</h2>{incidents_table}"
    )
    return _page(f"{monitor.name} - alerts dashboard", body)


def render_not_found(message: str = "Not found.") -> str:
    body = f'<div class="nav"><a href="/">&larr; all monitors</a></div><h1>404</h1><p>{escape(message)}</p>'
    return _page("Not found - alerts dashboard", body)
