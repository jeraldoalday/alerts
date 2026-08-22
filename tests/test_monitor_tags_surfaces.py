"""Tags everywhere they are reported: sync, status, dashboard, API, metrics.

A tag is only useful if the same one narrows every surface the same way, so
these check the filter and the rendering on each rather than trusting the
one shared query underneath.
"""
import json

import pytest

from alerts import cli
from alerts.config import MonitorConfig
from alerts.models import Monitor, MonitorState, MonitorType
from alerts.reporting import collect_status_rows, format_status_table
from alerts.scheduler import sync_monitors
from alerts.web import metrics, queries, render
from alerts.web.app import DashboardApp
from alerts.web.stats import compute_uptime_stats


def _monitor(session, name: str, tags=(), **overrides) -> Monitor:
    defaults = dict(
        name=name,
        type=MonitorType.HTTP,
        target=f"https://{name}.example.com",
        state=MonitorState.UP,
        tags=list(tags),
    )
    defaults.update(overrides)
    monitor = Monitor(**defaults)
    session.add(monitor)
    session.flush()
    return monitor


def _call(app, path: str, query: str = ""):
    status = []
    environ = {"REQUEST_METHOD": "GET", "PATH_INFO": path, "QUERY_STRING": query}
    body = b"".join(app(environ, lambda s, h: status.append(s)))
    return status[0], body.decode("utf-8")


def _config(name: str, tags) -> MonitorConfig:
    return MonitorConfig(
        name=name, type="http", target=f"https://{name}.example.com", tags=list(tags)
    )


# --- sync ---------------------------------------------------------------


def test_sync_writes_tags_on_a_new_monitor(db):
    with db.session() as session:
        sync_monitors(session, [_config("api", ["prod", "payments"])])

    with db.session() as session:
        assert queries.get_monitor_by_name(session, "api").tags == ["prod", "payments"]


def test_sync_updates_the_tags_of_an_existing_monitor(db):
    with db.session() as session:
        sync_monitors(session, [_config("api", ["prod"])])
    with db.session() as session:
        sync_monitors(session, [_config("api", ["staging"])])

    with db.session() as session:
        assert queries.get_monitor_by_name(session, "api").tags == ["staging"]


def test_sync_can_remove_every_tag(db):
    """Deleting the `tags:` key means untagged, not "leave what was there"."""
    with db.session() as session:
        sync_monitors(session, [_config("api", ["prod"])])
    with db.session() as session:
        sync_monitors(session, [_config("api", [])])

    with db.session() as session:
        assert queries.get_monitor_by_name(session, "api").tags == []


def test_a_monitor_stores_the_normalised_spelling(db):
    with db.session() as session:
        sync_monitors(session, [_config("api", ["PROD", " Prod "])])

    with db.session() as session:
        assert queries.get_monitor_by_name(session, "api").tags == ["prod"]


# --- queries ------------------------------------------------------------


def test_list_monitors_narrows_to_a_tag(db):
    with db.session() as session:
        _monitor(session, "api", ["prod"])
        _monitor(session, "staging-api", ["staging"])
        _monitor(session, "untagged")

    with db.session() as session:
        assert [m.name for m in queries.list_monitors(session, tag="prod")] == ["api"]


def test_list_monitors_without_a_tag_returns_everything(db):
    with db.session() as session:
        _monitor(session, "api", ["prod"])
        _monitor(session, "untagged")

    with db.session() as session:
        assert len(queries.list_monitors(session)) == 2


@pytest.mark.parametrize("asked", ["PROD", " prod ", "Prod"])
def test_the_tag_filter_folds_what_it_is_asked_for(db, asked):
    with db.session() as session:
        _monitor(session, "api", ["prod"])

    with db.session() as session:
        assert len(queries.list_monitors(session, tag=asked)) == 1


def test_known_tags_are_sorted_and_deduplicated(db):
    with db.session() as session:
        _monitor(session, "a", ["prod", "payments"])
        _monitor(session, "b", ["prod"])

    with db.session() as session:
        assert queries.known_tags(session) == ["payments", "prod"]


def test_known_tags_includes_a_tag_only_a_paused_monitor_still_carries(db):
    """Offering a filter that silently matches nothing would be worse."""
    with db.session() as session:
        _monitor(session, "gone", ["legacy"], enabled=False)

    with db.session() as session:
        assert queries.known_tags(session) == ["legacy"]


# --- alerts status ------------------------------------------------------


def test_the_status_table_shows_a_monitors_tags(db):
    with db.session() as session:
        _monitor(session, "api", ["prod", "payments"])

    with db.session() as session:
        table = format_status_table(collect_status_rows(session))

    assert "TAGS" in table
    assert "prod,payments" in table


def test_the_status_rows_can_be_narrowed_to_a_tag(db):
    with db.session() as session:
        _monitor(session, "api", ["prod"])
        _monitor(session, "staging-api", ["staging"])

    with db.session() as session:
        rows = collect_status_rows(session, tag="prod")

    assert [row.name for row in rows] == ["api"]


def test_an_untagged_monitor_leaves_the_column_empty(db):
    with db.session() as session:
        _monitor(session, "api")

    with db.session() as session:
        rows = collect_status_rows(session)

    assert rows[0].tags == ()


def test_status_command_filters_by_tag(tmp_path, capsys, db_config):
    config_path, database = db_config
    with database.session() as session:
        _monitor(session, "api", ["prod"])
        _monitor(session, "staging-api", ["staging"])

    assert cli.main(["status", "--config", config_path, "--tag", "prod"]) == 0

    out = capsys.readouterr().out
    assert "api" in out
    assert "staging-api" not in out


def test_status_command_reports_a_tag_that_matches_nothing(tmp_path, capsys, db_config):
    config_path, database = db_config
    with database.session() as session:
        _monitor(session, "api", ["prod"])

    assert cli.main(["status", "--config", config_path, "--tag", "nope"]) == 0

    out = capsys.readouterr().out
    assert "No monitor tagged 'nope'." in out
    assert "No monitors configured." not in out


def test_an_unmatched_tag_fails_the_gate(tmp_path, capsys, db_config):
    config_path, database = db_config
    with database.session() as session:
        _monitor(session, "api", ["prod"])

    assert cli.main(["status", "--config", config_path, "--tag", "nope", "--exit-code"]) == 1


def test_a_down_monitor_outside_the_tag_does_not_fail_the_gate(tmp_path, capsys, db_config):
    """A per-team gate must not trip on a service that team doesn't own."""
    config_path, database = db_config
    with database.session() as session:
        _monitor(session, "ours", ["payments"])
        _monitor(session, "theirs", ["search"], state=MonitorState.DOWN)

    exit_code = cli.main(
        ["status", "--config", config_path, "--tag", "payments", "--exit-code"]
    )

    assert exit_code == 0


# --- dashboard ----------------------------------------------------------


def test_the_index_lists_tags_as_filter_links(db):
    with db.session() as session:
        _monitor(session, "api", ["prod"])

    _, body = _call(DashboardApp(db), "/")

    assert 'href="/?tag=prod"' in body


def test_the_index_can_be_narrowed_by_query_string(db):
    with db.session() as session:
        _monitor(session, "api", ["prod"])
        _monitor(session, "staging-api", ["staging"])

    _, body = _call(DashboardApp(db), "/", "tag=prod")

    assert "api" in body
    assert "staging-api" not in body


def test_a_blank_tag_parameter_means_everything(db):
    """What a cleared filter control submits."""
    with db.session() as session:
        _monitor(session, "api", ["prod"])
        _monitor(session, "other", ["staging"])

    _, body = _call(DashboardApp(db), "/", "tag=")

    assert "api" in body and "other" in body


def test_the_filtered_index_names_the_tag_it_is_showing(db):
    with db.session() as session:
        _monitor(session, "api", ["prod"])

    _, body = _call(DashboardApp(db), "/", "tag=prod")

    assert "Monitors: prod" in body


def test_a_filtered_index_with_no_matches_says_which_tag(db):
    with db.session() as session:
        _monitor(session, "api", ["prod"])

    _, body = _call(DashboardApp(db), "/", "tag=nope")

    assert "No monitors tagged nope." in body
    assert "No monitors configured." not in body


def test_the_summary_tiles_count_the_filtered_set(db):
    with db.session() as session:
        _monitor(session, "ours", ["payments"])
        _monitor(session, "a", ["search"])
        _monitor(session, "b", ["search"])

    _, body = _call(DashboardApp(db), "/", "tag=payments")

    assert '<span class="count">1</span><span class="label">monitors</span>' in body


def test_a_tag_needing_encoding_is_encoded_in_its_link(db):
    with db.session() as session:
        _monitor(session, "api", ["a&b"])

    _, body = _call(DashboardApp(db), "/")

    assert 'href="/?tag=a%26b"' in body
    assert ">a&amp;b</a>" in body


def test_no_filter_row_when_nothing_is_tagged(db):
    with db.session() as session:
        _monitor(session, "api")

    _, body = _call(DashboardApp(db), "/")

    assert 'class="tags"' not in body


def test_the_detail_page_shows_a_monitors_tags(db):
    with db.session() as session:
        _monitor(session, "api", ["prod"])

    _, body = _call(DashboardApp(db), "/monitors/api")

    assert 'href="/?tag=prod"' in body


def test_render_tag_filter_marks_the_active_tag():
    html = render.render_tag_filter(["payments", "prod"], "prod")

    assert '<a class="tag current" href="/?tag=prod">prod</a>' in html
    assert '<a class="tag" href="/?tag=payments">payments</a>' in html


def test_render_tag_filter_marks_all_when_nothing_is_filtered():
    html = render.render_tag_filter(["prod"], None)

    assert '<a class="tag current" href="/">all</a>' in html


# --- JSON API -----------------------------------------------------------


def test_api_monitors_reports_tags_and_echoes_the_filter(db):
    with db.session() as session:
        _monitor(session, "api", ["prod", "payments"])
        _monitor(session, "staging-api", ["staging"])

    _, body = _call(DashboardApp(db), "/api/monitors", "tag=prod")

    payload = json.loads(body)
    assert payload["tag"] == "prod"
    assert [m["name"] for m in payload["monitors"]] == ["api"]
    assert payload["monitors"][0]["tags"] == ["prod", "payments"]


def test_api_monitor_detail_reports_tags(db):
    with db.session() as session:
        _monitor(session, "api", ["prod"])

    _, body = _call(DashboardApp(db), "/api/monitors/api")

    assert json.loads(body)["tags"] == ["prod"]


def test_api_summary_counts_only_the_tagged_monitors(db):
    with db.session() as session:
        _monitor(session, "ours", ["payments"])
        _monitor(session, "theirs", ["search"], state=MonitorState.DOWN)

    _, body = _call(DashboardApp(db), "/api/summary", "tag=payments")

    payload = json.loads(body)
    assert payload["total"] == 1
    assert payload["tag"] == "payments"
    assert payload["states"]["down"] == 0


def test_api_summary_open_incidents_stays_estate_wide(db):
    """The one figure whose job is not depending on which view you're in."""
    from alerts.models import Incident

    with db.session() as session:
        ours = _monitor(session, "ours", ["payments"])
        theirs = _monitor(session, "theirs", ["search"])
        session.add(Incident(monitor_id=theirs.id))
        assert ours.id != theirs.id

    _, body = _call(DashboardApp(db), "/api/summary", "tag=payments")

    assert json.loads(body)["open_incidents"] == 1


# --- metrics ------------------------------------------------------------


def _metrics_for(session) -> str:
    rows = [
        (monitor, compute_uptime_stats(queries.recent_check_results(session, monitor.id)))
        for monitor in queries.list_monitors(session)
    ]
    return metrics.render_metrics(rows, queries.count_open_incidents(session))


def test_metrics_export_one_series_per_tag(db):
    with db.session() as session:
        _monitor(session, "api", ["prod", "payments"])

    with db.session() as session:
        body = _metrics_for(session)

    assert 'alerts_monitor_tag{monitor="api",type="http",tag="prod"} 1' in body
    assert 'alerts_monitor_tag{monitor="api",type="http",tag="payments"} 1' in body


def test_the_tag_series_is_declared_once(db):
    with db.session() as session:
        _monitor(session, "a", ["prod"])
        _monitor(session, "b", ["prod"])

    with db.session() as session:
        body = _metrics_for(session)

    assert body.count("# TYPE alerts_monitor_tag gauge") == 1


def test_an_untagged_monitor_emits_no_tag_series(db):
    with db.session() as session:
        _monitor(session, "api")

    with db.session() as session:
        body = _metrics_for(session)

    assert "alerts_monitor_tag{" not in body


def test_a_paused_monitors_tags_are_not_exported(db):
    """A frozen reading a scraper can't tell from a live one, tags included."""
    with db.session() as session:
        _monitor(session, "gone", ["legacy"], enabled=False)

    with db.session() as session:
        body = _metrics_for(session)

    assert "legacy" not in body


def test_the_other_series_are_unchanged_in_shape(db):
    """Tags are a new series, not a new label on the existing ones."""
    with db.session() as session:
        _monitor(session, "api", ["prod"])

    with db.session() as session:
        body = _metrics_for(session)

    state_line = [
        line for line in body.splitlines() if line.startswith("alerts_monitor_state{")
    ][0]
    assert "tag=" not in state_line
