"""Builds per-channel notifier instances from the loaded config."""
from __future__ import annotations

from typing import Dict, List

from alerts.config import (
    ChannelConfig,
    EmailChannelConfig,
    SlackChannelConfig,
    WebhookChannelConfig,
)
from alerts.notifiers.base import Notifier
from alerts.notifiers.email import EmailNotifier
from alerts.notifiers.slack import SlackNotifier
from alerts.notifiers.webhook import WebhookNotifier


def build_notifier(channel: ChannelConfig) -> Notifier:
    if isinstance(channel, SlackChannelConfig):
        return SlackNotifier(channel.webhook_url)
    if isinstance(channel, WebhookChannelConfig):
        # Resolved here rather than at load time, so a token lives in the
        # notifier that needs it instead of in the config object everything
        # else in the process is holding a reference to.
        return WebhookNotifier(channel.resolved_url, headers=channel.resolved_headers)
    if isinstance(channel, EmailChannelConfig):
        return EmailNotifier(
            smtp_host=channel.smtp_host,
            smtp_port=channel.smtp_port,
            use_tls=channel.use_tls,
            from_addr=channel.from_addr,
            to_addrs=channel.to_addrs,
            username=channel.username,
            password=channel.password,
        )
    raise TypeError(f"unsupported channel config type: {type(channel)!r}")


def build_notifiers(channels: List[ChannelConfig]) -> Dict[str, Notifier]:
    return {channel.name: build_notifier(channel) for channel in channels}
