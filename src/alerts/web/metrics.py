"""Prometheus text-format rendering of the current monitor state.

A monitoring tool that can't be monitored is a single point of failure, so
the dashboard exposes the same picture it renders as HTML in the format
every metrics scraper already speaks. Written by hand rather than with a
client library: the exposition format is a handful of lines of text, and
the project's dependency list is deliberately short.

The format is documented at
https://prometheus.io/docs/instrumenting/exposition_formats/ -- one HELP
line and one TYPE line per metric family, then samples, then a trailing
newline. Everything here is a gauge: these are current readings, not
counters that only ever climb.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from alerts.models import Monitor, MonitorState
from alerts.slo import MAX_TARGET, BudgetStatus, ErrorBudget
from alerts.web.stats import UptimeStats

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

_LABEL_ESCAPES = (("\\", "\\\\"), ('"', '\\"'), ("\n", "\\n"))


def escape_label_value(value: str) -> str:
    """Escape a label value for the exposition format.

    Backslash first, or the escapes added for the other two would themselves
    be escaped a second time. Monitor names are operator-controlled but not
    validated for either character, and one unescaped quote would corrupt
    every sample after it in the response rather than just its own.
    """
    for old, new in _LABEL_ESCAPES:
        value = value.replace(old, new)
    return value


def _labels(pairs: Sequence[Tuple[str, str]]) -> str:
    if not pairs:
        return ""
    rendered = ",".join(f'{name}="{escape_label_value(value)}"' for name, value in pairs)
    return "{" + rendered + "}"


def _sample(name: str, value: float, labels: Sequence[Tuple[str, str]] = ()) -> str:
    return f"{name}{_labels(labels)} {_format_value(value)}"


def _format_value(value: float) -> str:
    """Render a number the way the exposition format expects.

    Integers lose their ".0" -- a state gauge reading "1" rather than "1.0"
    is what every example in the wild looks like -- and everything else is
    left to repr, which round-trips a float exactly.
    """
    if isinstance(value, bool):
        return "1" if value else "0"
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


def _quantile_label(percent: float) -> str:
    """A percentile as the 0-1 `quantile` label value Prometheus expects.

    The convention is a fraction ("0.95"), not a percentage ("95"), and
    dashboards written against other exporters filter on exactly that
    string. `_format_value` is reused so 50 renders as "0.5" rather than
    "0.5000000000000001".
    """
    return _format_value(percent / 100)


class _Family:
    """One metric family: its help text, its type, and its samples."""

    def __init__(self, name: str, help_text: str, metric_type: str = "gauge"):
        self.name = name
        self.help_text = help_text
        self.metric_type = metric_type
        self.samples: List[str] = []

    def add(self, value: float, labels: Sequence[Tuple[str, str]] = ()) -> None:
        self.samples.append(_sample(self.name, value, labels))

    def render(self) -> List[str]:
        return [
            f"# HELP {self.name} {self.help_text}",
            f"# TYPE {self.name} {self.metric_type}",
            *self.samples,
        ]


def render_metrics(
    rows: Iterable[Tuple[Monitor, UptimeStats]],
    open_incidents: int = 0,
    budgets: Optional[Mapping[str, ErrorBudget]] = None,
) -> str:
    """Render every monitor's current reading in the Prometheus text format.

    Paused monitors are skipped entirely rather than exported with stale
    numbers. A scraper has no way to tell a frozen reading from a live one,
    and a series that keeps reporting "down" for a monitor nobody checks any
    more would alert forever.

    `alerts_monitor_state` is exported once per state per monitor, with a
    `state` label and a 0/1 value, which is how a scraper expects an
    enum-valued gauge: it makes `alerts_monitor_state{state="down"} == 1` a
    working alert rule without needing to know the numeric encoding.

    Latency percentiles are exported the same way, as a gauge carrying a
    `quantile` label, rather than as a `summary`. A summary's quantiles are
    computed by the process being measured over its own lifetime; these are
    computed here over a fixed window of recent checks, and claiming the type
    would promise the `_sum` and `_count` series a summary comes with.

    Error budgets arrive as a mapping keyed by monitor name rather than as a
    third element of each row. They are computed over the monitor's own SLO
    window by a different query from the one behind `UptimeStats`, which
    covers a fixed count of recent checks -- folding two different windows
    into one tuple would suggest they are the same measurement. A monitor
    with no objective is simply absent from the mapping, and exports none of
    the `_slo_` series at all, so a scraper never sees a target nobody set.

    Tags are exported as their own `alerts_monitor_tag` series rather than as
    a label on the existing ones. A monitor carries any number of tags and a
    Prometheus label holds one value, so folding them in would mean either
    picking one arbitrarily or a comma-joined string nothing can match
    against. As a separate series they are joinable in the usual way:
    `alerts_monitor_state{state="down"} * on(monitor) group_left
    alerts_monitor_tag{tag="prod"}` alerts on one group without any of the
    other series changing shape.
    """
    state_family = _Family(
        "alerts_monitor_state",
        "Current state of a monitor (1 for the state it is in, 0 otherwise).",
    )
    uptime_family = _Family(
        "alerts_monitor_uptime_ratio",
        "Fraction of recent checks that succeeded, between 0 and 1.",
    )
    latency_family = _Family(
        "alerts_monitor_latency_ms",
        "Mean latency in milliseconds over the recent checks.",
    )
    checks_family = _Family(
        "alerts_monitor_checks",
        "Number of recent checks the uptime and latency figures cover.",
    )
    budget_family = _Family(
        "alerts_monitor_latency_budget_ms",
        "Latency at or above which a successful check counts as degraded.",
    )
    quantile_family = _Family(
        "alerts_monitor_latency_quantile_ms",
        "Latency percentile in milliseconds over the recent checks.",
    )
    tag_family = _Family(
        "alerts_monitor_tag",
        "1 for each tag a monitor carries, as a joinable label set.",
    )
    slo_target_family = _Family(
        "alerts_monitor_slo_target_ratio",
        "Availability a monitor is expected to hold, between 0 and 1.",
    )
    slo_window_family = _Family(
        "alerts_monitor_slo_window_days",
        "Length in days of the rolling window an SLO target is measured over.",
    )
    slo_availability_family = _Family(
        "alerts_monitor_slo_availability_ratio",
        "Measured availability over the SLO window, between 0 and 1.",
    )
    slo_consumed_family = _Family(
        "alerts_monitor_slo_budget_consumed_ratio",
        "Fraction of the error budget spent; above 1 means the target is missed.",
    )
    slo_status_family = _Family(
        "alerts_monitor_slo_status",
        "Standing against the SLO target (1 for the status it is in, 0 otherwise).",
    )
    budgets = budgets or {}

    exported = 0
    for monitor, stats in rows:
        if monitor.is_paused:
            continue
        exported += 1
        labels = [("monitor", monitor.name), ("type", monitor.type.value)]
        for state in MonitorState:
            state_family.add(
                1 if monitor.state == state else 0, [*labels, ("state", state.value)]
            )
        checks_family.add(stats.total_checks, labels)
        if stats.uptime_percentage is not None:
            uptime_family.add(stats.uptime_percentage / 100, labels)
        if stats.average_latency_ms is not None:
            latency_family.add(stats.average_latency_ms, labels)
        for percent, value in stats.latency_percentiles.items():
            if value is not None:
                quantile_family.add(value, [*labels, ("quantile", _quantile_label(percent))])
        if monitor.degraded_latency_ms is not None:
            budget_family.add(monitor.degraded_latency_ms, labels)
        for tag in monitor.tags or []:
            tag_family.add(1, [*labels, ("tag", tag)])

        budget = budgets.get(monitor.name)
        if budget is None:
            continue
        # The target and the window come from the config and are exported
        # whether or not any check has landed yet -- a dashboard drawing a
        # threshold line needs them before the first scrape has data to draw
        # it against. Everything measured is skipped while the window is
        # empty, for the reason the uptime series above is: a scraper cannot
        # tell an absent series from a real reading of zero, and zero
        # availability is the loudest thing this exposition can say.
        slo_target_family.add(budget.target_percentage / MAX_TARGET, labels)
        slo_window_family.add(budget.window_days, labels)
        for status in BudgetStatus:
            slo_status_family.add(
                1 if budget.status == status else 0, [*labels, ("status", status.value)]
            )
        if budget.actual_percentage is not None:
            slo_availability_family.add(budget.actual_percentage / MAX_TARGET, labels)
        if budget.consumed_ratio is not None:
            slo_consumed_family.add(budget.consumed_ratio, labels)

    monitors_family = _Family("alerts_monitors", "Number of monitors being checked.")
    monitors_family.add(exported)
    incidents_family = _Family(
        "alerts_open_incidents", "Number of incidents currently open."
    )
    incidents_family.add(open_incidents)

    lines: List[str] = []
    for family in (
        monitors_family,
        incidents_family,
        state_family,
        uptime_family,
        latency_family,
        quantile_family,
        checks_family,
        budget_family,
        tag_family,
        slo_target_family,
        slo_window_family,
        slo_availability_family,
        slo_consumed_family,
        slo_status_family,
    ):
        lines.extend(family.render())
    return "\n".join(lines) + "\n"


def state_values(monitor: Monitor) -> Dict[str, int]:
    """The 0/1 value this monitor exports for each state label.

    Exposed for tests and for anyone assembling the same picture elsewhere;
    `render_metrics` is the only caller in the app.
    """
    return {state.value: int(monitor.state == state) for state in MonitorState}


def parse_sample(line: str) -> Optional[Tuple[str, Dict[str, str], str]]:
    """Split one exposition line into its name, labels, and value.

    A comment or blank line returns None. Only good enough for tests and
    debugging -- it assumes the well-formed output this module produces
    rather than implementing the full grammar.
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    metric, _, value = line.rpartition(" ")
    name, brace, rest = metric.partition("{")
    labels: Dict[str, str] = {}
    if brace:
        for pair in rest.rstrip("}").split('",'):
            if not pair:
                continue
            key, _, raw = pair.partition("=")
            labels[key.strip()] = raw.strip().strip('"')
    return name, labels, value
