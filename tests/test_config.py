
import pytest
import yaml

from alerts.config import ConfigError, EmailChannelConfig, load_config

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


def _write_and_load(tmp_path, raw: dict):
    """Load a config from a dict, for cases where building YAML by hand is noise."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.safe_dump(raw))
    return load_config(config_file)


def test_load_valid_config(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(VALID_CONFIG)

    config = load_config(config_file)

    assert config.database.url == "sqlite:///./alerts.db"
    assert len(config.monitors) == 1
    assert config.monitors[0].name == "example-website"
    assert config.monitors[0].channels == ["ops-slack"]
    assert config.channels[0].name == "ops-slack"


def test_notifications_cooldown_defaults_when_unset(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(VALID_CONFIG)

    config = load_config(config_file)

    assert config.notifications.cooldown_seconds == 900


def test_notifications_cooldown_is_configurable(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(VALID_CONFIG + "\nnotifications:\n  cooldown_seconds: 60\n")

    config = load_config(config_file)

    assert config.notifications.cooldown_seconds == 60


def test_dashboard_defaults_when_unset(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(VALID_CONFIG)

    config = load_config(config_file)

    assert config.dashboard.enabled is True
    assert config.dashboard.host == "127.0.0.1"
    assert config.dashboard.port == 8080


def test_dashboard_settings_are_configurable(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        VALID_CONFIG + "\ndashboard:\n  enabled: false\n  host: 0.0.0.0\n  port: 9000\n"
    )

    config = load_config(config_file)

    assert config.dashboard.enabled is False
    assert config.dashboard.host == "0.0.0.0"
    assert config.dashboard.port == 9000


def test_dashboard_port_out_of_range_rejected(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(VALID_CONFIG + "\ndashboard:\n  port: 70000\n")

    with pytest.raises(ConfigError):
        load_config(config_file)


def test_scheduler_default_interval_backfills_monitors_that_omit_it(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
scheduler:
  default_interval_seconds: 45

monitors:
  - name: uses-default
    type: http
    target: https://a.example.com
  - name: overrides-default
    type: http
    target: https://b.example.com
    interval_seconds: 10
"""
    )

    config = load_config(config_file)

    by_name = {m.name: m for m in config.monitors}
    assert by_name["uses-default"].interval_seconds == 45
    assert by_name["overrides-default"].interval_seconds == 10


def test_scheduler_default_interval_falls_back_to_60_when_unset(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
monitors:
  - name: site
    type: http
    target: https://a.example.com
"""
    )

    config = load_config(config_file)

    assert config.monitors[0].interval_seconds == 60


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


def test_duplicate_channel_names_rejected(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
channels:
  - name: dup
    type: webhook
    url: https://a.example.com
  - name: dup
    type: webhook
    url: https://b.example.com
"""
    )

    with pytest.raises(ConfigError, match="duplicate channel names"):
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


def _email_channel(**overrides) -> EmailChannelConfig:
    defaults = dict(
        name="ops-email",
        type="email",
        smtp_host="smtp.example.com",
        from_addr="alerts@example.com",
        to_addrs=["oncall@example.com"],
    )
    defaults.update(overrides)
    return EmailChannelConfig(**defaults)


def test_email_channel_resolves_credentials_from_env(monkeypatch):
    monkeypatch.setenv("SMTP_USER", "bot")
    monkeypatch.setenv("SMTP_PASS", "secret")
    channel = _email_channel(username_env="SMTP_USER", password_env="SMTP_PASS")

    assert channel.username == "bot"
    assert channel.password == "secret"


def test_email_channel_credentials_are_none_without_env_vars():
    channel = _email_channel()

    assert channel.username is None
    assert channel.password is None


def test_monitor_degraded_latency_defaults_to_none(tmp_path):
    config = _write_and_load(
        tmp_path,
        {"monitors": [{"name": "m", "type": "http", "target": "https://example.com"}]},
    )

    assert config.monitors[0].degraded_latency_ms is None


def test_monitor_degraded_latency_is_loaded(tmp_path):
    config = _write_and_load(
        tmp_path,
        {
            "monitors": [
                {
                    "name": "m",
                    "type": "http",
                    "target": "https://example.com",
                    "timeout_seconds": 5,
                    "degraded_latency_ms": 1500,
                }
            ]
        },
    )

    assert config.monitors[0].degraded_latency_ms == 1500


def test_monitor_degraded_latency_must_be_positive(tmp_path):
    with pytest.raises(ConfigError):
        _write_and_load(
            tmp_path,
            {
                "monitors": [
                    {
                        "name": "m",
                        "type": "http",
                        "target": "https://example.com",
                        "degraded_latency_ms": 0,
                    }
                ]
            },
        )


def test_monitor_degraded_latency_above_timeout_is_rejected(tmp_path):
    """A budget the timeout can never let a check reach is a config mistake."""
    with pytest.raises(ConfigError) as excinfo:
        _write_and_load(
            tmp_path,
            {
                "monitors": [
                    {
                        "name": "slow-site",
                        "type": "http",
                        "target": "https://example.com",
                        "timeout_seconds": 2,
                        "degraded_latency_ms": 2000,
                    }
                ]
            },
        )

    assert "degraded_latency_ms" in str(excinfo.value)
