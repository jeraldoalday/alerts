import pytest

from alerts.config import EmailChannelConfig, SlackChannelConfig, WebhookChannelConfig
from alerts.notifiers.email import EmailNotifier
from alerts.notifiers.registry import build_notifier, build_notifiers
from alerts.notifiers.slack import SlackNotifier
from alerts.notifiers.webhook import WebhookNotifier


def test_build_notifier_returns_slack_notifier_for_slack_config(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.example/xyz")
    channel = SlackChannelConfig(
        name="ops-slack", type="slack", webhook_url_env="SLACK_WEBHOOK_URL", timeout_seconds=3
    )

    notifier = build_notifier(channel)

    assert isinstance(notifier, SlackNotifier)
    assert notifier._webhook_url == "https://hooks.slack.example/xyz"
    assert notifier._client.timeout.connect == 3


def test_build_notifier_returns_webhook_notifier_for_webhook_config():
    channel = WebhookChannelConfig(
        name="ops-webhook",
        type="webhook",
        url="https://hooks.example.com",
        headers={"X-Key": "v"},
        timeout_seconds=4,
    )

    notifier = build_notifier(channel)

    assert isinstance(notifier, WebhookNotifier)
    assert notifier._url == "https://hooks.example.com"
    assert notifier._headers == {"X-Key": "v"}
    assert notifier._client.timeout.connect == 4


def test_build_notifier_returns_email_notifier_for_email_config():
    channel = EmailChannelConfig(
        name="ops-email",
        type="email",
        smtp_host="smtp.example.com",
        from_addr="alerts@example.com",
        to_addrs=["oncall@example.com"],
        timeout_seconds=7,
    )

    notifier = build_notifier(channel)

    assert isinstance(notifier, EmailNotifier)
    assert notifier._smtp_host == "smtp.example.com"
    assert notifier._to_addrs == ["oncall@example.com"]
    assert notifier._timeout_seconds == 7


def test_build_notifiers_keys_by_channel_name(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.example/xyz")
    channels = [
        SlackChannelConfig(name="ops-slack", type="slack", webhook_url_env="SLACK_WEBHOOK_URL"),
        WebhookChannelConfig(name="ops-webhook", type="webhook", url="https://hooks.example.com"),
    ]

    notifiers = build_notifiers(channels)

    assert set(notifiers) == {"ops-slack", "ops-webhook"}
    assert isinstance(notifiers["ops-slack"], SlackNotifier)
    assert isinstance(notifiers["ops-webhook"], WebhookNotifier)


def test_build_notifier_rejects_unsupported_channel_type():
    with pytest.raises(TypeError):
        build_notifier(object())  # type: ignore[arg-type]
