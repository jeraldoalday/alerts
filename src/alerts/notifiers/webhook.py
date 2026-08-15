"""Sends notifications to a generic user-configured webhook URL."""
from __future__ import annotations

from typing import Dict, Optional

import httpx

from alerts.notifiers.base import NotifyResult


class WebhookNotifier:
    """POSTs `{"message": ...}` to an arbitrary URL with optional extra headers.

    Any 2xx/3xx response counts as success; unlike Slack's webhook there's no
    fixed payload shape the receiving end expects, so the message is sent as
    plain JSON rather than a service-specific format.
    """

    def __init__(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self._url = url
        self._headers = headers or {}
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None

    async def send(self, message: str) -> NotifyResult:
        try:
            response = await self._client.post(
                self._url, json={"message": message}, headers=self._headers
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            return NotifyResult(success=False, error_message=str(exc) or type(exc).__name__)
        return NotifyResult(success=True)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
