import httpx

from alerts.notifiers.webhook import WebhookNotifier


def _notifier(handler, **kwargs) -> WebhookNotifier:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return WebhookNotifier("https://hooks.example.com/alerts", client=client, **kwargs)


async def test_webhook_notifier_posts_message_payload_and_succeeds():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        return httpx.Response(204)

    notifier = _notifier(handler)

    result = await notifier.send("api-gateway is down")

    assert result.success is True
    assert b"api-gateway is down" in seen["body"]


async def test_webhook_notifier_sends_configured_headers():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = request.headers
        return httpx.Response(200)

    notifier = _notifier(handler, headers={"X-Api-Key": "secret"})

    await notifier.send("api-gateway is down")

    assert seen["headers"]["X-Api-Key"] == "secret"


async def test_webhook_notifier_reports_non_2xx_as_failure():
    notifier = _notifier(lambda request: httpx.Response(500, text="boom"))

    result = await notifier.send("api-gateway is down")

    assert result.success is False
    assert result.error_message is not None


async def test_aclose_closes_a_self_owned_client():
    notifier = WebhookNotifier("https://hooks.example.com/alerts")

    await notifier.aclose()

    assert notifier._client.is_closed is True


async def test_aclose_leaves_an_injected_client_open():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    notifier = WebhookNotifier("https://hooks.example.com/alerts", client=client)

    await notifier.aclose()

    assert client.is_closed is False
    await client.aclose()
