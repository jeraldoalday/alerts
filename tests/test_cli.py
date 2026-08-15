from alerts import cli
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
