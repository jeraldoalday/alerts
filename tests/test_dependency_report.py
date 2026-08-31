"""The `alerts deps` report: row collection, rendering, and the subcommand.

The estate under test is the one the feature exists for: a checkout page
and an API that both need a database, with a marketing site off to the
side that needs nothing. What varies is which of them is down, and
therefore which rows claim an alert is being held back.

The distinction worth defending is between "something upstream is down"
and "an alert is being suppressed". A healthy service above a broken
database is the first without being the second, and a report that
conflated them would tell an operator that pages are being swallowed when
there are none to send.
"""
from alerts import cli
from alerts.db import Database
from alerts.models import Monitor, MonitorState, MonitorType
from alerts.reporting import (
    DependencyRow,
    collect_dependency_rows,
    format_dependency_summary,
    format_dependency_table,
)
from alerts.web.queries import dependency_graph


def _monitor(session, name, state=MonitorState.UP, depends_on=(), **overrides):
    monitor = Monitor(
        name=name,
        type=MonitorType.HTTP,
        target=f"https://{name}.example.com",
        state=state,
        depends_on=list(depends_on),
        **overrides,
    )
    session.add(monitor)
    return monitor


def _estate(session, db_state=MonitorState.UP, api_state=MonitorState.UP):
    _monitor(session, "checkout", MonitorState.DOWN, ["api"])
    _monitor(session, "api", api_state, ["db"])
    _monitor(session, "db", db_state)
    _monitor(session, "marketing")


def _row(**overrides) -> DependencyRow:
    defaults = dict(name="checkout", state=MonitorState.DOWN, depends_on=("api",))
    defaults.update(overrides)
    return DependencyRow(**defaults)


def _config(tmp_path):
    path = tmp_path / "config.yaml"
    db_path = tmp_path / "deps.db"
    path.write_text(f"database:\n  url: sqlite:///{db_path}\n")
    return str(path), f"sqlite:///{db_path}"


def test_a_monitor_with_no_edges_is_left_out(db):
    with db.session() as session:
        _estate(session)

    with db.session() as session:
        names = [row.name for row in collect_dependency_rows(session)]

    assert "marketing" not in names
    assert names == ["db", "api", "checkout"]


def test_rows_come_out_upstreams_first(db):
    """So the chain reads down the page in the direction the failure travels."""
    with db.session() as session:
        _estate(session)

    with db.session() as session:
        rows = collect_dependency_rows(session)

    assert [row.name for row in rows] == ["db", "api", "checkout"]


def test_a_row_carries_what_the_monitor_declared(db):
    with db.session() as session:
        _estate(session)

    with db.session() as session:
        rows = {row.name: row for row in collect_dependency_rows(session)}

    assert rows["checkout"].depends_on == ("api",)
    assert rows["db"].depends_on == ()


def test_nothing_is_blocked_while_every_upstream_is_healthy(db):
    with db.session() as session:
        _estate(session)

    with db.session() as session:
        rows = collect_dependency_rows(session)

    assert all(not row.is_blocked for row in rows)
    assert all(not row.alerts_held_back for row in rows)


def test_a_down_upstream_is_reported_transitively(db):
    with db.session() as session:
        _estate(session, db_state=MonitorState.DOWN)

    with db.session() as session:
        rows = {row.name: row for row in collect_dependency_rows(session)}

    assert rows["checkout"].blocked_by == ["db"]
    assert rows["api"].blocked_by == ["db"]


def test_a_healthy_monitor_over_a_broken_upstream_is_not_held_back(db):
    """It has nothing to page about, so nothing is being suppressed."""
    with db.session() as session:
        _estate(session, db_state=MonitorState.DOWN)

    with db.session() as session:
        rows = {row.name: row for row in collect_dependency_rows(session)}

    assert rows["api"].is_blocked
    assert not rows["api"].alerts_held_back
    assert rows["checkout"].alerts_held_back


def test_the_root_cause_counts_only_the_dependents_actually_kept_quiet(db):
    with db.session() as session:
        _estate(session, db_state=MonitorState.DOWN)

    with db.session() as session:
        rows = {row.name: row for row in collect_dependency_rows(session)}

    # checkout is down and suppressed; api is up and has nothing to send.
    assert rows["db"].is_root_cause
    assert rows["db"].dependents_held_back == 1


def test_a_monitor_blocked_by_something_else_is_not_the_root_cause(db):
    with db.session() as session:
        _estate(session, db_state=MonitorState.DOWN, api_state=MonitorState.DOWN)

    with db.session() as session:
        rows = {row.name: row for row in collect_dependency_rows(session)}

    assert rows["db"].is_root_cause
    assert not rows["api"].is_root_cause
    assert rows["api"].alerts_held_back


def test_a_paused_upstream_blocks_nothing(db):
    with db.session() as session:
        _estate(session, db_state=MonitorState.DOWN)
        session.query(Monitor).filter_by(name="db").one().enabled = False

    with db.session() as session:
        rows = {row.name: row for row in collect_dependency_rows(session)}

    assert rows["checkout"].blocked_by == []
    assert not rows["db"].is_root_cause


def test_filtering_by_monitor_keeps_its_whole_neighbourhood(db):
    with db.session() as session:
        _estate(session)

    with db.session() as session:
        names = [row.name for row in collect_dependency_rows(session, monitor="api")]

    assert names == ["db", "api", "checkout"]


def test_filtering_by_a_monitor_outside_the_graph_reports_nothing(db):
    with db.session() as session:
        _estate(session)

    with db.session() as session:
        assert collect_dependency_rows(session, monitor="marketing") == []


def test_the_graph_is_available_on_its_own_for_the_dashboard(db):
    with db.session() as session:
        _estate(session)

    with db.session() as session:
        graph = dependency_graph(session)

    assert graph.upstream("checkout") == ["api", "db"]


def test_the_table_names_its_columns():
    table = format_dependency_table([_row()])

    assert "MONITOR" in table
    assert "UPSTREAM DOWN" in table
    assert "checkout" in table


def test_a_monitor_with_nothing_down_upstream_renders_a_dash():
    table = format_dependency_table([_row()])

    assert " -" in table


def test_a_held_back_row_says_so():
    table = format_dependency_table([_row(blocked_by=("db",))])

    assert "alerts held back" in table


def test_a_healthy_row_over_a_down_upstream_claims_nothing():
    table = format_dependency_table([_row(state=MonitorState.UP, blocked_by=("db",))])

    assert "held back" not in table
    assert "db" in table


def test_a_root_cause_row_says_how_much_is_waiting_on_it():
    row = _row(name="db", depends_on=(), dependents_held_back=3)

    assert "root cause, 3 dependent(s) held back" in format_dependency_table([row])


def test_a_paused_row_says_so_before_anything_else():
    row = _row(paused=True, blocked_by=("db",))

    assert "paused, not in config" in format_dependency_table([row])


def test_an_empty_table_says_nothing_declares_a_dependency():
    assert format_dependency_table([]) == "No monitor declares a dependency."


def test_the_summary_counts_the_graph_and_what_it_is_holding_back():
    rows = [
        _row(name="db", depends_on=(), dependents_held_back=1),
        _row(name="checkout", blocked_by=("db",)),
    ]

    summary = format_dependency_summary(rows)

    assert "2 monitor(s) in the dependency graph" in summary
    assert "1 held back" in summary
    assert "1 root cause(s)" in summary


def test_the_summary_reports_zero_held_back_rather_than_staying_quiet():
    """The reassurance someone reading this during an incident came for."""
    summary = format_dependency_summary([_row(state=MonitorState.UP)])

    assert "0 held back" in summary
    assert "root cause" not in summary


def test_main_deps_prints_a_table_and_a_summary(tmp_path, capsys):
    config_path, database_url = _config(tmp_path)
    database = Database(database_url)
    database.create_all()
    with database.session() as session:
        _estate(session, db_state=MonitorState.DOWN)

    exit_code = cli.main(["deps", "--config", config_path])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "MONITOR" in output
    assert "checkout" in output
    assert "root cause" in output


def test_main_deps_exit_code_fails_while_an_alert_is_held_back(tmp_path, capsys):
    config_path, database_url = _config(tmp_path)
    database = Database(database_url)
    database.create_all()
    with database.session() as session:
        _estate(session, db_state=MonitorState.DOWN)

    assert cli.main(["deps", "--config", config_path]) == 0, "non-zero exit is opt-in"
    assert cli.main(["deps", "--config", config_path, "--exit-code"]) == 1
    capsys.readouterr()


def test_main_deps_exit_code_is_zero_when_the_estate_is_healthy(tmp_path, capsys):
    config_path, database_url = _config(tmp_path)
    database = Database(database_url)
    database.create_all()
    with database.session() as session:
        _estate(session)

    assert cli.main(["deps", "--config", config_path, "--exit-code"]) == 0
    capsys.readouterr()


def test_main_deps_on_an_estate_with_no_dependencies(tmp_path, capsys):
    config_path, _ = _config(tmp_path)

    assert cli.main(["deps", "--config", config_path]) == 0
    assert "No monitor declares a dependency." in capsys.readouterr().out


def test_main_deps_filtered_to_a_monitor_outside_the_graph(tmp_path, capsys):
    config_path, database_url = _config(tmp_path)
    database = Database(database_url)
    database.create_all()
    with database.session() as session:
        _estate(session)

    exit_code = cli.main(["deps", "--config", config_path, "--monitor", "marketing"])

    assert exit_code == 0
    assert "is part of the dependency graph" in capsys.readouterr().out


def test_main_deps_filtered_to_nothing_fails_under_exit_code(tmp_path, capsys):
    """A gate that passes because a name was mistyped is the failure worth catching."""
    config_path, database_url = _config(tmp_path)
    database = Database(database_url)
    database.create_all()
    with database.session() as session:
        _estate(session)

    assert cli.main(["deps", "--config", config_path, "--monitor", "typo", "--exit-code"]) == 1
    capsys.readouterr()


def test_main_deps_on_a_config_that_has_never_been_synced(tmp_path, capsys):
    path = tmp_path / "config.yaml"
    db_path = tmp_path / "deps.db"
    path.write_text(
        f"""
database:
  url: sqlite:///{db_path}
monitors:
  - name: web
    type: http
    target: https://example.com
    depends_on: [api]
  - name: api
    type: http
    target: https://api.example.com
"""
    )

    exit_code = cli.main(["deps", "--config", str(path)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Run `alerts run` to sync the config." in output


def test_deps_arguments_are_parsed():
    args = cli._parse_args(["deps", "--monitor", "api", "--exit-code"])

    assert args.monitor == "api"
    assert args.exit_code is True
