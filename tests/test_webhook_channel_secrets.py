"""`${VAR}` expansion in webhook channel URLs and headers.

The webhook channel is the one without a dedicated `*_env` field, and the
one whose receiver most often wants a token -- sometimes in a header,
sometimes in the URL path itself. These cover both, from config validation
through to what the built notifier actually holds.
"""
import pytest
import yaml

from alerts.config import ConfigError, WebhookChannelConfig, load_config
from alerts.envsubst import MissingEnvVar
from alerts.notifiers.registry import build_notifier


def _channel(**overrides) -> WebhookChannelConfig:
    defaults = dict(name="ops-webhook", type="webhook", url="https://hooks.example.com")
    defaults.update(overrides)
    return WebhookChannelConfig(**defaults)


def _write_and_load(tmp_path, raw: dict):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.safe_dump(raw))
    return load_config(config_file)


def _config_with_channel(**channel):
    body = dict(name="hook", type="webhook")
    body.update(channel)
    return {"channels": [body]}


# --- resolution ------------------------------------------------------------


def test_a_literal_url_is_unchanged():
    assert _channel().resolved_url == "https://hooks.example.com"


def test_a_url_placeholder_is_expanded(monkeypatch):
    """Plenty of receivers put the secret in the path rather than a header."""
    monkeypatch.setenv("HOOK_TOKEN", "t0ken")

    channel = _channel(url="https://hooks.example.com/${HOOK_TOKEN}")

    assert channel.resolved_url == "https://hooks.example.com/t0ken"


def test_a_header_placeholder_is_expanded(monkeypatch):
    monkeypatch.setenv("HOOK_KEY", "abc123")

    channel = _channel(headers={"X-Api-Key": "${HOOK_KEY}"})

    assert channel.resolved_headers == {"X-Api-Key": "abc123"}


def test_literal_headers_still_work():
    assert _channel(headers={"X-Source": "alerts"}).resolved_headers == {"X-Source": "alerts"}


def test_no_headers_resolves_to_an_empty_mapping():
    assert _channel().resolved_headers == {}


def test_resolving_a_missing_variable_raises(monkeypatch):
    monkeypatch.delenv("GONE_HOOK", raising=False)

    with pytest.raises(MissingEnvVar):
        _channel(url="https://hooks.example.com/${GONE_HOOK}").resolved_url


def test_the_stored_config_keeps_the_placeholder(monkeypatch):
    """Expansion is on access, so the config object never holds the secret."""
    monkeypatch.setenv("HOOK_TOKEN", "t0ken")
    channel = _channel(url="https://hooks.example.com/${HOOK_TOKEN}")

    channel.resolved_url

    assert channel.url == "https://hooks.example.com/${HOOK_TOKEN}"


# --- problem reporting -----------------------------------------------------


def test_nothing_to_resolve_reports_no_problems():
    assert _channel(headers={"X-Source": "alerts"}).env_placeholder_problems() == []


def test_a_resolvable_placeholder_reports_no_problems(monkeypatch):
    monkeypatch.setenv("HOOK_KEY", "abc")

    assert _channel(headers={"X-Api-Key": "${HOOK_KEY}"}).env_placeholder_problems() == []


def test_problems_cover_the_url_and_the_headers(monkeypatch):
    monkeypatch.delenv("URL_VAR", raising=False)
    monkeypatch.delenv("HEADER_VAR", raising=False)

    channel = _channel(
        url="https://hooks.example.com/${URL_VAR}", headers={"X-Api-Key": "${HEADER_VAR}"}
    )

    assert channel.env_placeholder_problems() == ["URL_VAR", "HEADER_VAR"]


def test_a_variable_named_twice_is_reported_once(monkeypatch):
    monkeypatch.delenv("SAME", raising=False)

    channel = _channel(url="https://x/${SAME}", headers={"A": "${SAME}"})

    assert channel.env_placeholder_problems() == ["SAME"]


# --- config loading --------------------------------------------------------


def test_load_config_accepts_resolvable_placeholders(tmp_path, monkeypatch):
    monkeypatch.setenv("HOOK_TOKEN", "t0ken")

    config = _write_and_load(
        tmp_path, _config_with_channel(url="https://hooks.example.com/${HOOK_TOKEN}")
    )

    assert config.channels[0].resolved_url == "https://hooks.example.com/t0ken"


def test_load_config_rejects_an_unresolvable_url(tmp_path, monkeypatch):
    monkeypatch.delenv("GONE_HOOK", raising=False)

    with pytest.raises(ConfigError, match="GONE_HOOK"):
        _write_and_load(
            tmp_path, _config_with_channel(url="https://hooks.example.com/${GONE_HOOK}")
        )


def test_load_config_rejects_an_unresolvable_header(tmp_path, monkeypatch):
    monkeypatch.delenv("GONE_KEY", raising=False)

    with pytest.raises(ConfigError, match="GONE_KEY"):
        _write_and_load(
            tmp_path,
            _config_with_channel(
                url="https://hooks.example.com", headers={"X-Api-Key": "${GONE_KEY}"}
            ),
        )


def test_a_slack_channel_is_left_to_its_own_env_field(tmp_path, monkeypatch):
    """Slack resolves webhook_url_env on access and raises naming it itself."""
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.example/xyz")

    config = _write_and_load(
        tmp_path,
        {
            "channels": [
                {"name": "ops", "type": "slack", "webhook_url_env": "SLACK_WEBHOOK_URL"}
            ]
        },
    )

    assert config.channels[0].webhook_url == "https://hooks.slack.example/xyz"


# --- the built notifier ----------------------------------------------------


def test_the_notifier_is_built_with_the_resolved_values(monkeypatch):
    monkeypatch.setenv("HOOK_TOKEN", "t0ken")
    monkeypatch.setenv("HOOK_KEY", "abc123")

    notifier = build_notifier(
        _channel(
            url="https://hooks.example.com/${HOOK_TOKEN}", headers={"X-Api-Key": "${HOOK_KEY}"}
        )
    )

    assert notifier._url == "https://hooks.example.com/t0ken"
    assert notifier._headers == {"X-Api-Key": "abc123"}
