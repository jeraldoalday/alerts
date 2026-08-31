"""HTML rendering for the dashboard.

Deliberately dependency-free: no template engine, just `str` building with
`html.escape` on every piece of data that ultimately comes from a monitor
config or a checker's error message (both operator-controlled, but never
trusted as safe-to-embed markup).
"""
from __future__ import annotations

from html import escape
from typing import List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote

from alerts.models import CheckResult, Incident, IncidentState, Monitor, MonitorState
from alerts.slo import BudgetStatus, BudgetSummary, ErrorBudget, summarize_budgets
from alerts.web.stats import StateSummary, UptimeStats

_STATE_COLORS = {
    MonitorState.UP: "#1a7f37",
    MonitorState.DEGRADED: "#9a6700",
    MonitorState.DOWN: "#cf222e",
    MonitorState.UNKNOWN: "#57606a",
}

# Deliberately the same three colours the states use. A budget running low
# is the same kind of warning as a degraded monitor, and a missed target the
# same kind of alarm as a down one; giving them a second palette would make
# a reader learn two colour schemes for one page.
_BUDGET_COLORS = {
    BudgetStatus.OK: _STATE_COLORS[MonitorState.UP],
    BudgetStatus.AT_RISK: _STATE_COLORS[MonitorState.DEGRADED],
    BudgetStatus.BREACHED: _STATE_COLORS[MonitorState.DOWN],
    BudgetStatus.NO_DATA: _STATE_COLORS[MonitorState.UNKNOWN],
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
.note { color: #57606a; font-size: 0.78rem; margin-top: 0.2rem; }
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


def _dependency_note(monitor: Monitor, blocked_by: Sequence[str]) -> str:
    """How a monitor's dependency situation is worded, wherever it is shown.

    Two different sentences, because they are two different facts. A
    monitor that is down with something down underneath it is having its
    alerts held back, and that is why nobody was paged. A healthy monitor
    over the same broken upstream is not being suppressed at all -- saying
    so would tell an operator that pages are being swallowed when there
    are none to send -- so its row reports the fact and stops there.
    """
    if not blocked_by:
        return ""
    chain = escape(", ".join(blocked_by))
    if monitor.alerts_held_back_by(blocked_by):
        return f"alerts held back &middot; {chain} down"
    return f"{chain} down"


def _blocked_marker(monitor: Monitor, blocked_by: Sequence[str]) -> str:
    """The dependency note under a state badge in the index table."""
    note = _dependency_note(monitor, blocked_by)
    return f'<div class="note">{note}</div>' if note else ""


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


def _budget_cell(budget: Optional[ErrorBudget]) -> str:
    """One monitor's standing against its target, for the index table.

    Both halves are shown -- the promise and what is left of the budget --
    because either alone is unreadable. "65% left" says nothing without
    knowing 65% of what, and a bare "99.9%" is the target being confused for
    the achievement, which is the specific misreading an SLO column exists
    to prevent.

    A monitor with no objective gets an em dash rather than a blank cell, so
    a column of them reads as "nothing promised here" rather than as a
    rendering that failed.
    """
    if budget is None:
        return '<span class="muted">&mdash;</span>'
    target = f"{budget.target_percentage:.2f}%"
    remaining = budget.remaining_ratio
    if remaining is None:
        detail = "no data"
    elif remaining <= 0:
        detail = "spent"
    else:
        detail = f"{remaining * 100:.0f}% left"
    color = _BUDGET_COLORS[budget.status]
    return (
        f'{escape(target)} <span style="color:{color}">&middot; {escape(detail)}</span>'
    )


def _budget_detail(budget: Optional[ErrorBudget]) -> str:
    """The SLO block on a monitor's own page.

    Rendered only for a monitor that has a target: a page that says "no SLO
    configured" on every one of forty services is a page nobody reads to the
    bottom of.

    The figures are the same ones `alerts slo` prints, in the same order, so
    someone moving between the two surfaces is reading one report rather
    than reconciling two.
    """
    if budget is None:
        return ""
    color = _BUDGET_COLORS[budget.status]
    parts = [
        f'<span class="badge" style="background:{color}">'
        f"{escape(budget.status.value)}</span>",
        f"&nbsp; target {budget.target_percentage:.2f}% over {budget.window_days}d",
    ]
    if budget.actual_percentage is not None:
        parts.append(f"&nbsp; measured {budget.actual_percentage:.2f}%")
    parts.append(f"&nbsp; over {budget.total_checks} check(s)")
    remaining = budget.remaining_downtime_seconds
    if remaining is not None:
        parts.append(
            f'<br><span class="muted">{_format_seconds(remaining)} of '
            f"{_format_seconds(budget.allowed_downtime_seconds)} of downtime "
            "budget left</span>"
        )
    return f"<h2>Objective</h2><p>{''.join(parts)}</p>"


def _format_seconds(value: float) -> str:
    """A compact duration for the budget line.

    A local, deliberately simple spelling rather than an import from the
    reporting module: the dashboard doesn't otherwise depend on the CLI
    tables, and one shared helper would tie a web page's wording to a
    terminal report's column widths.
    """
    seconds = max(int(value), 0)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


def _summary_tile(label: str, count: int, color: Optional[str] = None) -> str:
    style = f' style="color:{color}"' if color and count else ""
    return (
        '<li class="tile">'
        f'<span class="count"{style}>{count}</span>'
        f'<span class="label">{escape(label)}</span>'
        "</li>"
    )


def render_summary(
    summary: StateSummary,
    budgets: Optional[BudgetSummary] = None,
    held_back: int = 0,
) -> str:
    """A row of at-a-glance tiles: one per monitor state, plus open incidents.

    Rendered even when every monitor is up -- an operator opening the
    dashboard mid-incident shouldn't have to count table rows to find out how
    much is broken, and "0 down" is itself the reassurance they came for.

    The SLO tiles follow the same two rules the state tiles do. "breached" is
    shown whenever anything has an objective at all, because a zero there is
    the answer someone came to the page for; "at risk" appears only when it
    is non-zero, the way "degraded" does, since a budget with room left in it
    is not news. An estate that has promised nothing gets neither, rather
    than a reassuring pair of zeroes about promises nobody made.
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
    if budgets is not None and budgets.total:
        if budgets.at_risk:
            tiles.append(
                _summary_tile(
                    "slo at risk", budgets.at_risk, _BUDGET_COLORS[BudgetStatus.AT_RISK]
                )
            )
        tiles.append(
            _summary_tile(
                "slo breached", budgets.breaching, _BUDGET_COLORS[BudgetStatus.BREACHED]
            )
        )
    if held_back:
        # Only when it is non-zero, the way "degraded" and "at risk" are. A
        # zero here is not reassurance; it is the normal state of an estate
        # that has never declared a dependency, and a permanent
        # "0 alerts held back" tile would be a tile about nothing.
        tiles.append(
            _summary_tile("alerts held back", held_back, _STATE_COLORS[MonitorState.DOWN])
        )
    if summary.paused:
        tiles.append(_summary_tile("paused", summary.paused))
    return f'<ul class="summary">{"".join(tiles)}</ul>'


def render_index(
    rows: Sequence[Tuple[Monitor, UptimeStats, object]],
    summary: Optional[StateSummary] = None,
    tags: Sequence[str] = (),
    active_tag: Optional[str] = None,
    budgets: Optional[Mapping[str, ErrorBudget]] = None,
    blocked: Optional[Mapping[str, Sequence[str]]] = None,
) -> str:
    """The monitor table.

    Down upstreams arrive as a mapping keyed by monitor name, like the
    budgets and for the same reason: a monitor with nothing down above it
    is simply absent. They are rendered under the state badge rather than
    in a column of their own -- the table is already nine columns wide, and
    the fact is only ever interesting for the handful of rows that have
    one.

    Budgets arrive as a mapping keyed by monitor name rather than as a
    fourth element of each row: they are measured over the monitor's own SLO
    window, while the uptime beside them covers a fixed count of recent
    checks, and putting two different windows in one tuple would suggest
    they are the same reading. A monitor with no objective is absent from
    the mapping and gets an em dash.
    """
    budgets = budgets or {}
    blocked = blocked or {}
    body_rows: List[str] = []
    for monitor, stats, last_checked_at in rows:
        body_rows.append(
            "<tr>"
            f'<td><a href="/monitors/{_url_path(monitor.name)}">{escape(monitor.name)}</a>'
            f"{_paused_marker(monitor)}</td>"
            f"<td>{escape(monitor.type.value)}</td>"
            f"<td>{_tag_links(monitor, active_tag)}</td>"
            f"<td>{_state_badge(monitor.state)}"
            f"{_blocked_marker(monitor, blocked.get(monitor.name, ()))}</td>"
            f"<td>{_format_percentage(stats.uptime_percentage)}</td>"
            f"<td>{_format_latency(stats.average_latency_ms)}</td>"
            f"<td>{_format_latency(stats.p95_latency_ms)}</td>"
            f"<td>{_budget_cell(budgets.get(monitor.name))}</td>"
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
    empty_row = f'<tr><td colspan="9" class="muted">{empty_message}</td></tr>'
    table = (
        "<table><thead><tr><th>Monitor</th><th>Type</th><th>Tags</th><th>State</th>"
        "<th>Uptime</th><th>Avg latency</th><th>p95 latency</th><th>SLO</th>"
        "<th>Last checked</th></tr></thead>"
        f"<tbody>{''.join(body_rows) or empty_row}</tbody>"
        "</table>"
    )
    # Paused monitors are left out of the budget tally, matching /api/summary
    # and /metrics: nothing is checking them, so their window slides forward
    # over a history that stopped growing.
    budget_summary = summarize_budgets(
        budgets.get(monitor.name) for monitor, _, _ in rows if not monitor.is_paused
    )
    held_back = sum(
        1
        for monitor, _, _ in rows
        if monitor.alerts_held_back_by(blocked.get(monitor.name, ()))
    )
    summary_html = (
        render_summary(summary, budget_summary, held_back) if summary is not None else ""
    )
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


def _monitor_links(names: Sequence[str]) -> str:
    """A list of monitor names, each linking to its own page."""
    return ", ".join(
        f'<a href="/monitors/{_url_path(name)}">{escape(name)}</a>' for name in names
    )


def _dependency_detail(
    monitor: Monitor,
    depends_on: Sequence[str],
    needed_by: Sequence[str],
    blocked_by: Sequence[str],
) -> str:
    """The dependency block on a monitor's own page.

    Rendered only for a monitor that is part of the graph, for the reason
    the SLO block above it is: a section reading "depends on nothing" on
    every one of forty services is a section nobody reads past.

    Both directions are shown. What a service needs explains why it is
    quiet; what needs it is the blast radius an operator wants the moment
    they are paged about this one.
    """
    if not depends_on and not needed_by:
        return ""
    lines = []
    if depends_on:
        lines.append(f"depends on {_monitor_links(depends_on)}")
    if needed_by:
        lines.append(f"needed by {_monitor_links(needed_by)}")
    note = _dependency_note(monitor, blocked_by)
    if note:
        lines.append(
            f'<span style="color:{_STATE_COLORS[MonitorState.DOWN]}">{note}</span>'
        )
    return "<h2>Dependencies</h2><p>" + "<br>".join(lines) + "</p>"


def render_monitor_detail(
    monitor: Monitor,
    stats: UptimeStats,
    check_results: Sequence[CheckResult],
    incidents: Sequence[Incident],
    budget: Optional[ErrorBudget] = None,
    depends_on: Sequence[str] = (),
    needed_by: Sequence[str] = (),
    blocked_by: Sequence[str] = (),
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
        f"{_budget_detail(budget)}"
        f"{_dependency_detail(monitor, depends_on, needed_by, blocked_by)}"
        f"<h2>Recent checks</h2>{checks_table}"
        f"<h2>Incidents</h2>{incidents_table}"
    )
    return _page(f"{monitor.name} - alerts dashboard", body)


def render_not_found(message: str = "Not found.") -> str:
    body = f'<div class="nav"><a href="/">&larr; all monitors</a></div><h1>404</h1><p>{escape(message)}</p>'
    return _page("Not found - alerts dashboard", body)
