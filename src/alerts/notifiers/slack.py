"""Sends notifications to a Slack incoming webhook."""
from __future__ import annotations

from typing import Optional

import httpx

from alerts.notifiers.base import NotifyResult


class SlackNotifier:
    """Posts `message` as Slack's `{"text": ...}` incoming-webhook payload.

    Reuses one `httpx.AsyncClient` across calls unless a client is injected,
    matching the connection-pooling pattern `HttpChecker` uses.
    """

    def __init__(self, webhook_url: str, client: Optional[httpx.AsyncClient] = None):
        self._webhook_url = webhook_url
        self._client = client or httpx.AsyncClient()
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
