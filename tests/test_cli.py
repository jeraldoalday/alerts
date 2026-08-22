from datetime import datetime, timedelta, timezone

from alerts import cli
from alerts.db import Database
from alerts.models import CheckResult, Monitor, MonitorState, MonitorType
from alerts.notifiers.slack import SlackNotifier


def _write_config(tmp_path) -> str:
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "test.db"
    config_file.write_text(f"database:\n  url: sqlite:///{db_path}\n")
    return str(config_file)


def test_parse_args_run_defaults_to_config_yaml():
    args = cli._parse_args(["run"])

    assert args.command == "run"
    assert args.config == "config.yaml"


def test_parse_args_run_accepts_custom_config_path():
    args = cli._parse_args(["run", "--config", "other.yaml"])

    assert args.config == "other.yaml"


def test_parse_args_serve_defaults_to_config_yaml():
    args = cli._parse_args(["serve"])

    assert args.command == "serve"
    assert args.config == "config.yaml"


def test_parse_args_serve_accepts_custom_config_path():
    args = cli._parse_args(["serve", "--config", "other.yaml"])

    assert args.config == "other.yaml"


def test_main_serve_builds_database_and_calls_serve_with_dashboard_settings(tmp_path, monkeypatch):
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "test.db"
    config_file.write_text(
        f"""
database:
  url: sqlite:///{db_path}

dashboard:
  host: 0.0.0.0
  port: 9090
"""
    )

    captured = {}

    def fake_serve(database, host, port):
        captured["database"] = database
        captured["host"] = host
        captured["port"] = port

    monkeypatch.setattr(cli, "serve", fake_serve)

    cli.main(["serve", "--config", str(config_file)])

    assert isinstance(captured["database"], cli.Database)
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 9090


def test_main_run_completes_when_no_monitors_are_configured(tmp_path):
    config_path = _write_config(tmp_path)

    # No monitors -> Scheduler.run() has no per-monitor loops to gather, so
    # this returns immediately instead of running forever.
    cli.main(["run", "--config", config_path])


def test_main_swallows_keyboard_interrupt(tmp_path, monkeypatch):
    config_path = _write_config(tmp_path)

    def fake_asyncio_run(coro):
        coro.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.asyncio, "run", fake_asyncio_run)

    cli.main(["run", "--config", config_path])  # should not raise


async def test_run_builds_notifiers_from_configured_channels(tmp_path, monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.example/xyz")
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "test.db"
    config_file.write_text(
        f"""
database:
  url: sqlite:///{db_path}

channels:
  - name: ops-slack
    type: slack
    webhook_url_env: SLACK_WEBHOOK_URL
"""
    )

    captured = {}
    real_scheduler_cls = cli.Scheduler

    def capturing_scheduler(*args, **kwargs):
        captured["notifiers"] = kwargs.get("notifiers")
        captured["cooldown_seconds"] = kwargs.get("cooldown_seconds")
        return real_scheduler_cls(*args, **kwargs)

    monkeypatch.setattr(cli, "Scheduler", capturing_scheduler)

    await cli._run(str(config_file))

    assert isinstance(captured["notifiers"]["ops-slack"], SlackNotifier)
    assert captured["cooldown_seconds"] == 900


def test_parse_args_prune_defaults():
    args = cli._parse_args(["prune"])

    assert args.command == "prune"
    assert args.config == "config.yaml"
    assert args.dry_run is False


def test_parse_args_prune_accepts_dry_run():
    args = cli._parse_args(["prune", "--config", "other.yaml", "--dry-run"])

    assert args.config == "other.yaml"
    assert args.dry_run is True


def _seed_old_check(database_url: str) -> None:
    database = Database(database_url)
    database.create_all()
    with database.session() as session:
        monitor = Monitor(name="site", type=MonitorType.HTTP, target="https://example.com")
        session.add(monitor)
        session.flush()
        session.add(
            CheckResult(
                monitor_id=monitor.id,
                success=True,
                checked_at=datetime.now(timezone.utc).replace(tzinfo=None)
                - timedelta(days=365),
            )
        )


def test_main_prune_deletes_aged_out_history(tmp_path):
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "test.db"
    database_url = f"sqlite:///{db_path}"
    config_file.write_text(
        f"database:\n  url: {database_url}\n"
        "retention:\n  check_results_days: 30\n  min_check_results_per_monitor: 0\n"
    )
    _seed_old_check(database_url)

    cli.main(["prune", "--config", str(config_file)])

    with Database(database_url).session() as session:
        assert session.query(CheckResult).count() == 0


def test_main_prune_dry_run_leaves_history_alone(tmp_path):
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "test.db"
    database_url = f"sqlite:///{db_path}"
    config_file.write_text(
        f"database:\n  url: {database_url}\n"
        "retention:\n  check_results_days: 30\n  min_check_results_per_monitor: 0\n"
    )
    _seed_old_check(database_url)

    cli.main(["prune", "--config", str(config_file), "--dry-run"])

    with Database(database_url).session() as session:
        assert session.query(CheckResult).count() == 1


def _status_config(tmp_path) -> tuple:
    config_file = tmp_path / "config.yaml"
    database_url = f"sqlite:///{tmp_path / 'test.db'}"
    config_file.write_text(f"database:\n  url: {database_url}\n")
    return str(config_file), database_url


def test_parse_args_status_defaults():
    args = cli._parse_args(["status"])

    assert args.command == "status"
    assert args.config == "config.yaml"
    assert args.checks == 50
    assert args.exit_code is False


def test_main_status_prints_a_table_and_a_summary(tmp_path, capsys):
    config_path, database_url = _status_config(tmp_path)
    database = Database(database_url)
    database.create_all()
    with database.session() as session:
        monitor = Monitor(
            name="site",
            type=MonitorType.HTTP,
            target="https://example.com",
            state=MonitorState.UP,
        )
        session.add(monitor)
        session.flush()
        session.add(CheckResult(monitor_id=monitor.id, success=True, latency_ms=12.0))

    exit_code = cli.main(["status", "--config", config_path])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "MONITOR" in output
    assert "site" in output
    assert "1 monitor(s): 1 up" in output


def test_main_status_exit_code_flag_reports_a_down_monitor(tmp_path, capsys):
    config_path, database_url = _status_config(tmp_path)
    database = Database(database_url)
    database.create_all()
    with database.session() as session:
        session.add(
            Monitor(
                name="site",
                type=MonitorType.HTTP,
                target="https://example.com",
                state=MonitorState.DOWN,
            )
        )

    assert cli.main(["status", "--config", config_path]) == 0, "non-zero exit is opt-in"
    assert cli.main(["status", "--config", config_path, "--exit-code"]) == 1
    capsys.readouterr()


def test_main_status_on_an_empty_database(tmp_path, capsys):
    config_path, _ = _status_config(tmp_path)

    assert cli.main(["status", "--config", config_path, "--exit-code"]) == 0
    assert "No monitors configured." in capsys.readouterr().out


def test_logging_setup_quiets_httpxs_per_request_lines():
    """One line per check per interval, forever, buries everything worth reading."""
    import logging

    cli._configure_logging()

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_logging_setup_leaves_the_projects_own_loggers_alone():
    """Only the transport is quieted; nothing sets a level on our own loggers.

    Asserted as "no level of its own" rather than as an effective level,
    which under pytest's logging plugin is whatever the plugin configured
    the root to rather than what this function did.
    """
    import logging

    cli._configure_logging()

    for name in ("alerts", "alerts.scheduler", "alerts.cli"):
        assert logging.getLogger(name).level == logging.NOTSET


def test_a_transport_warning_still_gets_through(caplog):
    """Lowered, not silenced -- a warning from the transport is worth seeing."""
    import logging

    cli._configure_logging()

    with caplog.at_level(logging.WARNING, logger="httpx"):
        logging.getLogger("httpx").warning("connection pool exhausted")

    assert "connection pool exhausted" in caplog.text
