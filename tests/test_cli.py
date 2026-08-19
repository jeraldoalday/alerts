from datetime import datetime, timedelta, timezone

from alerts import cli
from alerts.db import Database
from alerts.models import CheckResult, Monitor, MonitorType
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
