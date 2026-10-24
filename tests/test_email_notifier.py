import smtplib

from alerts.notifiers.email import EmailNotifier


class _FakeSmtp:
    """Stands in for `smtplib.SMTP` as a context manager, recording calls."""

    instances = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.starttls_called = False
        self.login_args = None
        self.sent_messages = []
        _FakeSmtp.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def starttls(self):
        self.starttls_called = True

    def login(self, username, password):
        self.login_args = (username, password)

    def send_message(self, message):
        self.sent_messages.append(message)


def _notifier(monkeypatch, **overrides) -> EmailNotifier:
    _FakeSmtp.instances = []
    monkeypatch.setattr(smtplib, "SMTP", _FakeSmtp)
    defaults = dict(
        smtp_host="smtp.example.com",
        smtp_port=587,
        use_tls=True,
        from_addr="alerts@example.com",
        to_addrs=["oncall@example.com"],
    )
    defaults.update(overrides)
    return EmailNotifier(**defaults)


async def test_email_notifier_sends_and_reports_success(monkeypatch):
    notifier = _notifier(monkeypatch)

    result = await notifier.send("site is down")

    assert result.success is True
    assert result.error_message is None
    smtp = _FakeSmtp.instances[0]
    assert smtp.host == "smtp.example.com"
    assert smtp.starttls_called is True
    assert len(smtp.sent_messages) == 1
    assert smtp.sent_messages[0]["To"] == "oncall@example.com"
    assert "site is down" in smtp.sent_messages[0].get_content()


async def test_email_notifier_passes_configured_timeout_to_smtp(monkeypatch):
    notifier = _notifier(monkeypatch, timeout_seconds=3.5)

    await notifier.send("site is down")

    assert _FakeSmtp.instances[0].timeout == 3.5


async def test_email_notifier_skips_starttls_when_disabled(monkeypatch):
    notifier = _notifier(monkeypatch, use_tls=False)

    await notifier.send("site is down")

    assert _FakeSmtp.instances[0].starttls_called is False


async def test_email_notifier_logs_in_when_credentials_configured(monkeypatch):
    notifier = _notifier(monkeypatch, username="bot", password="hunter2")

    await notifier.send("site is down")

    assert _FakeSmtp.instances[0].login_args == ("bot", "hunter2")


async def test_email_notifier_skips_login_without_credentials(monkeypatch):
    notifier = _notifier(monkeypatch)

    await notifier.send("site is down")

    assert _FakeSmtp.instances[0].login_args is None


async def test_email_notifier_reports_smtp_errors_as_failure(monkeypatch):
    class _RaisingSmtp(_FakeSmtp):
        def send_message(self, message):
            raise smtplib.SMTPException("mailbox unavailable")

    monkeypatch.setattr(smtplib, "SMTP", _RaisingSmtp)
    notifier = EmailNotifier(
        smtp_host="smtp.example.com",
        smtp_port=587,
        use_tls=True,
        from_addr="alerts@example.com",
        to_addrs=["oncall@example.com"],
    )

    result = await notifier.send("site is down")

    assert result.success is False
    assert "mailbox unavailable" in result.error_message


async def test_email_notifier_reports_connection_errors_as_failure(monkeypatch):
    def _raise(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(smtplib, "SMTP", _raise)
    notifier = EmailNotifier(
        smtp_host="smtp.example.com",
        smtp_port=587,
        use_tls=True,
        from_addr="alerts@example.com",
        to_addrs=["oncall@example.com"],
    )

    result = await notifier.send("site is down")

    assert result.success is False
    assert "connection refused" in result.error_message
