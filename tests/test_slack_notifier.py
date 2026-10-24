import httpx

from alerts.notifiers.slack import SlackNotifier


def _notifier(handler) -> SlackNotifier:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return SlackNotifier("https://hooks.slack.example/T000/B000/xyz", client=client)


async def test_slack_notifier_posts_text_payload_and_succeeds():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx.Response(200, text="ok")

    notifier = _notifier(handler)

    result = await notifier.send("site is down")

    assert result.success is True
    assert result.error_message is None
    assert seen["url"] == "https://hooks.slack.example/T000/B000/xyz"
    assert b"site is down" in seen["body"]


async def test_slack_notifier_reports_non_2xx_as_failure():
    notifier = _notifier(lambda request: httpx.Response(404, text="no such webhook"))

    result = await notifier.send("site is down")

    assert result.success is False
    assert result.error_message is not None


async def test_slack_notifier_reports_connection_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    notifier = _notifier(handler)

    result = await notifier.send("site is down")

    assert result.success is False
    assert "connection refused" in result.error_message


async def test_self_owned_client_uses_configured_timeout():
    notifier = SlackNotifier(
        "https://hooks.slack.example/T000/B000/xyz", timeout_seconds=3.5
    )

    assert notifier._client.timeout.connect == 3.5

    await notifier.aclose()


async def test_aclose_closes_a_self_owned_client():
    notifier = SlackNotifier("https://hooks.slack.example/T000/B000/xyz")

    await notifier.aclose()

    assert notifier._client.is_closed is True


async def test_aclose_leaves_an_injected_client_open():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    notifier = SlackNotifier("https://hooks.slack.example/T000/B000/xyz", client=client)

    await notifier.aclose()

    assert client.is_closed is False
    await client.aclose()
