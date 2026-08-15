"""Sends notifications by SMTP email."""
from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage
from typing import List, Optional

from alerts.notifiers.base import NotifyResult


class EmailNotifier:
    """Sends `message` as a plaintext email via SMTP.

    `smtplib` is synchronous and blocking, so `send` runs it in a worker
    thread via `asyncio.to_thread` rather than blocking the event loop that
    the scheduler and other notifiers share.
    """

    def __init__(
        self,
        *,
        smtp_host: str,
        smtp_port: int,
        use_tls: bool,
        from_addr: str,
        to_addrs: List[str],
        username: Optional[str] = None,
        password: Optional[str] = None,
        subject: str = "alerts notification",
        timeout_seconds: float = 10.0,
    ):
        self._smtp_host = smtp_host
        self._smtp_port = smtp_port
        self._use_tls = use_tls
        self._from_addr = from_addr
        self._to_addrs = to_addrs
        self._username = username
        self._password = password
        self._subject = subject
        self._timeout_seconds = timeout_seconds

    async def send(self, message: str) -> NotifyResult:
        try:
            await asyncio.to_thread(self._send_sync, message)
        except (smtplib.SMTPException, OSError) as exc:
            return NotifyResult(success=False, error_message=str(exc) or type(exc).__name__)
        return NotifyResult(success=True)

    def _send_sync(self, message: str) -> None:
        email_message = EmailMessage()
        email_message["Subject"] = self._subject
        email_message["From"] = self._from_addr
        email_message["To"] = ", ".join(self._to_addrs)
        email_message.set_content(message)

        with smtplib.SMTP(self._smtp_host, self._smtp_port, timeout=self._timeout_seconds) as smtp:
            if self._use_tls:
                smtp.starttls()
            if self._username and self._password:
                smtp.login(self._username, self._password)
            smtp.send_message(email_message)
