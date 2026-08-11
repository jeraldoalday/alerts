from alerts import cli


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
