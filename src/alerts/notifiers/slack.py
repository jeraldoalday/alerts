"""Sends notifications to a Slack incoming webhook."""
from __future__ import annotations

from typing import Optional

import httpx

from alerts.notifiers.base import NotifyResult


class SlackNotifier:
    """Posts `message` as Slack's `{"text": ...}` incoming-webhook payload.

    Reuses one `httpx.AsyncClient` across calls unless a client is injected,
    matching the connection-pooling pattern `HttpChecker` uses. `timeout_seconds`
    only applies to a self-owned client -- an injected one (tests, or a client
    shared with another notifier) keeps whatever timeout it was built with.
    """

    def __init__(
        self,
        webhook_url: str,
        client: Optional[httpx.AsyncClient] = None,
        timeout_seconds: float = 10.0,
    ):
        self._webhook_url = webhook_url
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_client = client is None

    async def send(self, message: str) -> NotifyResult:
        try:
            response = await self._client.post(self._webhook_url, json={"text": message})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            return NotifyResult(success=False, error_message=str(exc) or type(exc).__name__)
        return NotifyResult(success=True)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
