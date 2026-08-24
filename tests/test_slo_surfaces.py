"""SLO figures on every surface that reports them.

The point of these is agreement. One promise measured over one window has
to read the same in `alerts slo`, on the index, on a monitor's own page, in
the JSON, and in the exposition -- so most of what follows checks that the
same database produces the same number in each of the five places rather
than checking each rendering in isolation.
"""
import json
from datetime import timedelta

from alerts.models import CheckResult, Monitor, MonitorState, MonitorType
from alerts.reporting import collect_slo_rows
from alerts.slo import BudgetStatus, compute_error_budget, summarize_budgets
from alerts.timeutil import utcnow
from alerts.web import queries, render
from alerts.web.app import DashboardApp
from alerts.web.metrics import parse_sample, render_metrics
from alerts.web.stats import UptimeStats

NOW = utcnow().replace(tzinfo=None)

_NO_STATS = UptimeStats(
    total_checks=0, successful_checks=0, uptime_percentage=None, average_latency_ms=None
)


def _monitor(session=None, name="api", **overrides) -> Monitor:
    fields = dict(
        name=name,
        type=MonitorType.HTTP,
        target="https://example.com",
        state=MonitorState.UP,
        slo_target=99.0,
        slo_window_days=30,
        tags=[],
        enabled=True,
    )
    fields.update(overrides)
    monitor = Monitor(**fields)
    if session is not None:
        session.add(monitor)
        session.flush()
    return monitor


def _checks(session, monitor_id, *, total, failed):
    for index in range(total):
        session.add(
            CheckResult(
                monitor_id=monitor_id,
                success=index >= failed,
                latency_ms=20.0,
                checked_at=NOW - timedelta(minutes=index),
            )
        )


def _budget(target=99.0, total=1000, failed=1, window_days=30):
    return compute_error_budget(
        target_percentage=target,
        total_checks=total,
        failed_checks=failed,
        window_days=window_days,
    )


def _call(app, path: str, query: str = ""):
    captured = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = dict(headers)

    environ = {"REQUEST_METHOD": "GET", "PATH_INFO": path, "QUERY_STRING": query}
    body = b"".join(app(environ, start_response))
    return captured["status"], body.decode("utf-8")


def _samples(text: str, name: str):
    parsed = [parse_sample(line) for line in text.splitlines()]
    return [p for p in parsed if p is not None and p[0] == name]


# --- the budget summary ----------------------------------------------------


def test_a_summary_tallies_budgets_by_status():
    summary = summarize_budgets(
        [
            _budget(total=1000, failed=0),
            _budget(total=1000, failed=8),
            _budget(total=1000, failed=80),
        ]
    )

    assert summary.total == 3
    assert summary.counts[BudgetStatus.OK] == 1
    assert summary.counts[BudgetStatus.AT_RISK] == 1
    assert summary.counts[BudgetStatus.BREACHED] == 1


def test_a_summary_skips_monitors_without_an_objective():
    """Callers map straight over their monitors, Nones and all."""
    summary = summarize_budgets([_budget(), None, None])

    assert summary.total == 1


def test_a_summary_carries_every_status_including_the_empty_ones():
    summary = summarize_budgets([_budget()])

    assert set(summary.counts) == set(BudgetStatus)
    assert summary.counts[BudgetStatus.BREACHED] == 0


def test_an_estate_with_no_objectives_is_not_reported_as_all_met():
    """There is no promise being kept, which is not the same as keeping one."""
    assert summarize_budgets([None, None]).all_met is False
    assert summarize_budgets([_budget()]).all_met is True
    assert summarize_budgets([_budget(failed=80)]).all_met is False


# --- the index page --------------------------------------------------------


def test_the_index_shows_the_target_and_what_is_left_of_the_budget():
    html = render.render_index(
        [(_monitor(), _NO_STATS, None)], budgets={"api": _budget(total=1000, failed=0)}
    )

    assert "99.00%" in html
    assert "100% left" in html


def test_the_index_shows_an_em_dash_for_a_monitor_with_no_objective():
    html = render.render_index([(_monitor(slo_target=None), _NO_STATS, None)])

    assert "<th>SLO</th>" in html
    assert "&mdash;" in html


def test_the_index_says_spent_rather_than_zero_percent_for_an_overrun_budget():
    html = render.render_index(
        [(_monitor(), _NO_STATS, None)], budgets={"api": _budget(total=1000, failed=50)}
    )

    assert "spent" in html


def test_the_index_says_no_data_for_an_empty_window():
    html = render.render_index(
        [(_monitor(), _NO_STATS, None)], budgets={"api": _budget(total=0, failed=0)}
    )

    assert "no data" in html


def test_a_breaching_budget_is_coloured_like_a_down_monitor():
    """One palette for the page, not two -- see _BUDGET_COLORS."""
    html = render.render_index(
        [(_monitor(), _NO_STATS, None)], budgets={"api": _budget(total=1000, failed=50)}
    )

    assert "#cf222e" in html


# --- the monitor page ------------------------------------------------------


def test_the_detail_page_shows_the_objective_block():
    html = render.render_monitor_detail(
        _monitor(), _NO_STATS, [], [], _budget(total=1000, failed=1)
    )

    assert "<h2>Objective</h2>" in html
    assert "target 99.00% over 30d" in html
    assert "measured 99.90%" in html
    assert "over 1000 check(s)" in html


def test_the_detail_page_translates_the_budget_into_minutes():
    html = render.render_monitor_detail(
        _monitor(), _NO_STATS, [], [], _budget(target=99.9, total=1000, failed=0)
    )

    assert "43m of 43m of downtime budget left" in html


def test_the_detail_page_omits_the_block_for_a_monitor_with_no_objective():
    """Forty pages each saying 'no SLO configured' is a page nobody finishes."""
    html = render.render_monitor_detail(_monitor(), _NO_STATS, [], [])

    assert "Objective" not in html


def test_the_detail_page_omits_the_measured_figure_with_no_data():
    html = render.render_monitor_detail(
        _monitor(), _NO_STATS, [], [], _budget(total=0, failed=0)
    )

    assert "<h2>Objective</h2>" in html
    assert "measured" not in html


# --- the exposition --------------------------------------------------------


def test_the_target_is_exported_as_a_ratio():
    text = render_metrics([(_monitor(), _NO_STATS)], budgets={"api": _budget()})

    assert _samples(text, "alerts_monitor_slo_target_ratio")[0][2] == "0.99"


def test_the_window_is_exported_in_days():
    text = render_metrics(
        [(_monitor(), _NO_STATS)], budgets={"api": _budget(window_days=7)}
    )

    assert _samples(text, "alerts_monitor_slo_window_days")[0][2] == "7"


def test_measured_availability_is_exported_as_a_ratio():
    text = render_metrics(
        [(_monitor(), _NO_STATS)], budgets={"api": _budget(total=1000, failed=10)}
    )

    value = float(_samples(text, "alerts_monitor_slo_availability_ratio")[0][2])
    assert value == 0.99


def test_the_consumed_ratio_is_exported_unclipped():
    """Above 1 is the alert rule, so it must not be capped on the way out."""
    text = render_metrics(
        [(_monitor(), _NO_STATS)], budgets={"api": _budget(total=1000, failed=30)}
    )

    value = float(_samples(text, "alerts_monitor_slo_budget_consumed_ratio")[0][2])
    assert value > 1.0


def test_the_status_is_exported_as_a_gauge_per_status_label():
    text = render_metrics(
        [(_monitor(), _NO_STATS)], budgets={"api": _budget(total=1000, failed=80)}
    )

    values = {
        labels["status"]: value
        for _, labels, value in _samples(text, "alerts_monitor_slo_status")
    }
    assert values == {"ok": "0", "at-risk": "0", "breached": "1", "no-data": "0"}


def test_the_target_is_exported_before_any_check_has_landed():
    """A dashboard needs the threshold line before it has data to draw it on."""
    text = render_metrics(
        [(_monitor(), _NO_STATS)], budgets={"api": _budget(total=0, failed=0)}
    )

    assert _samples(text, "alerts_monitor_slo_target_ratio")
    assert not _samples(text, "alerts_monitor_slo_availability_ratio")
    assert not _samples(text, "alerts_monitor_slo_budget_consumed_ratio")


def test_a_monitor_with_no_objective_exports_no_slo_series():
    text = render_metrics([(_monitor(slo_target=None), _NO_STATS)])

    assert not _samples(text, "alerts_monitor_slo_target_ratio")
    assert not _samples(text, "alerts_monitor_slo_status")


def test_the_slo_series_carry_the_same_labels_as_the_rest():
    text = render_metrics(
        [(_monitor(name="api-gateway"), _NO_STATS)],
        budgets={"api-gateway": _budget()},
    )

    _, labels, _ = _samples(text, "alerts_monitor_slo_target_ratio")[0]
    assert labels == {"monitor": "api-gateway", "type": "http"}


def test_the_slo_families_declare_help_and_type():
    text = render_metrics([(_monitor(), _NO_STATS)], budgets={"api": _budget()})

    for family in (
        "alerts_monitor_slo_target_ratio",
        "alerts_monitor_slo_window_days",
        "alerts_monitor_slo_availability_ratio",
        "alerts_monitor_slo_budget_consumed_ratio",
        "alerts_monitor_slo_status",
    ):
        assert f"# HELP {family} " in text
        assert f"# TYPE {family} gauge" in text


# --- the JSON API ----------------------------------------------------------


def test_api_monitors_carries_the_slo_object(db):
    with db.session() as session:
        monitor = _monitor(session)
        _checks(session, monitor.id, total=1000, failed=1)

    _, body = _call(DashboardApp(db), "/api/monitors")

    slo = json.loads(body)["monitors"][0]["slo"]
    assert slo["target_percentage"] == 99.0
    assert slo["window_days"] == 30
    assert slo["total_checks"] == 1000
    assert slo["failed_checks"] == 1
    assert slo["status"] == "ok"


def test_api_monitors_sends_null_for_a_monitor_with_no_objective(db):
    with db.session() as session:
        _monitor(session, slo_target=None)

    _, body = _call(DashboardApp(db), "/api/monitors")

    assert json.loads(body)["monitors"][0]["slo"] is None


def test_api_monitor_detail_carries_the_same_slo_object(db):
    with db.session() as session:
        monitor = _monitor(session)
        _checks(session, monitor.id, total=1000, failed=1)

    _, listing = _call(DashboardApp(db), "/api/monitors")
    _, detail = _call(DashboardApp(db), "/api/monitors/api")

    assert json.loads(listing)["monitors"][0]["slo"] == json.loads(detail)["slo"]


def test_the_slo_object_sends_both_the_ratio_and_the_seconds(db):
    with db.session() as session:
        monitor = _monitor(session, slo_target=99.9)
        _checks(session, monitor.id, total=1000, failed=0)

    _, body = _call(DashboardApp(db), "/api/monitors/api")

    slo = json.loads(body)["slo"]
    assert slo["budget_consumed_ratio"] == 0.0
    assert slo["budget_remaining_ratio"] == 1.0
    assert slo["allowed_downtime_seconds"] == 2592.0
    assert slo["remaining_downtime_seconds"] == 2592.0


def test_api_summary_tallies_objectives(db):
    with db.session() as session:
        healthy = _monitor(session, "healthy", slo_target=99.0)
        breaching = _monitor(session, "breaching", slo_target=99.0)
        _monitor(session, "unpromised", slo_target=None)
        _checks(session, healthy.id, total=1000, failed=0)
        _checks(session, breaching.id, total=1000, failed=100)

    _, body = _call(DashboardApp(db), "/api/summary")

    slo = json.loads(body)["slo"]
    assert slo["with_target"] == 2
    assert slo["statuses"]["ok"] == 1
    assert slo["statuses"]["breached"] == 1


def test_api_summary_leaves_paused_monitors_out_of_the_tally(db):
    """Their window slides over a history that stopped growing."""
    with db.session() as session:
        paused = _monitor(session, "paused", slo_target=99.0, enabled=False)
        _checks(session, paused.id, total=1000, failed=900)

    _, body = _call(DashboardApp(db), "/api/summary")

    slo = json.loads(body)["slo"]
    assert slo["with_target"] == 0
    assert slo["statuses"]["breached"] == 0


def test_api_summary_narrowed_by_tag_tallies_only_that_group(db):
    with db.session() as session:
        web = _monitor(session, "web", slo_target=99.0, tags=["prod"])
        batch = _monitor(session, "batch", slo_target=99.0, tags=["internal"])
        _checks(session, web.id, total=1000, failed=0)
        _checks(session, batch.id, total=1000, failed=500)

    _, body = _call(DashboardApp(db), "/api/summary", "tag=prod")

    slo = json.loads(body)["slo"]
    assert slo["with_target"] == 1
    assert slo["statuses"]["breached"] == 0


# --- agreement between the surfaces ----------------------------------------


def test_every_surface_reports_the_same_measured_availability(db):
    with db.session() as session:
        monitor = _monitor(session, slo_target=99.0)
        _checks(session, monitor.id, total=1000, failed=5)

    with db.session() as session:
        row = collect_slo_rows(session)[0]
        budget = queries.error_budget(session, queries.get_monitor_by_name(session, "api"))

    _, body = _call(DashboardApp(db), "/api/monitors/api")
    text = render_metrics([(_monitor(), _NO_STATS)], budgets={"api": budget})
    exported = float(_samples(text, "alerts_monitor_slo_availability_ratio")[0][2])

    assert row.budget.actual_percentage == 99.5
    assert json.loads(body)["slo"]["actual_percentage"] == 99.5
    assert exported == 0.995


def test_the_index_page_and_the_report_agree_about_the_status(db):
    with db.session() as session:
        monitor = _monitor(session, slo_target=99.0)
        _checks(session, monitor.id, total=1000, failed=100)

    with db.session() as session:
        row = collect_slo_rows(session)[0]

    _, html = _call(DashboardApp(db), "/")

    assert row.status is BudgetStatus.BREACHED
    assert "spent" in html
