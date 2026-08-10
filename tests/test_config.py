
import pytest

from alerts.config import ConfigError, load_config

VALID_CONFIG = """
database:
  url: sqlite:///./alerts.db

monitors:
  - name: example-website
    type: http
    target: https://example.com
    interval_seconds: 30
    channels: [ops-slack]

channels:
  - name: ops-slack
    type: slack
    webhook_url_env: SLACK_WEBHOOK_URL
"""


def test_load_valid_config(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(VALID_CONFIG)

    config = load_config(config_file)

    assert config.database.url == "sqlite:///./alerts.db"
    assert len(config.monitors) == 1
    assert config.monitors[0].name == "example-website"
    assert config.monitors[0].channels == ["ops-slack"]
    assert config.channels[0].name == "ops-slack"


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.yaml")


def test_invalid_yaml_raises(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("monitors: [this is not: valid: yaml")

    with pytest.raises(ConfigError):
        load_config(config_file)


def test_duplicate_monitor_names_rejected(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
monitors:
  - name: dup
    type: http
    target: https://a.example.com
  - name: dup
    type: http
    target: https://b.example.com
"""
    )

    with pytest.raises(ConfigError, match="duplicate monitor names"):
        load_config(config_file)


def test_monitor_referencing_unknown_channel_rejected(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
monitors:
  - name: example-website
    type: http
    target: https://example.com
    channels: [does-not-exist]
"""
    )

    with pytest.raises(ConfigError, match="unknown channel"):
        load_config(config_file)


def test_monitor_name_with_space_rejected(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
monitors:
  - name: "bad name"
    type: http
    target: https://example.com
"""
    )

    with pytest.raises(ConfigError):
        load_config(config_file)


def test_slack_channel_resolves_webhook_from_env(tmp_path, monkeypatch):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(VALID_CONFIG)
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/abc")

    config = load_config(config_file)

    assert config.channels[0].webhook_url == "https://hooks.slack.test/abc"


def test_slack_channel_missing_env_raises(tmp_path, monkeypatch):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(VALID_CONFIG)
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)

    config = load_config(config_file)

    with pytest.raises(ConfigError, match="SLACK_WEBHOOK_URL"):
        _ = config.channels[0].webhook_url
