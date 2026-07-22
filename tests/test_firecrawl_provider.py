import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest

from app.acquisition.providers import ProviderFailure, ProviderRequest
from app.acquisition.sessions import SessionHandle
from app.url_safety import UnsafeUrlError


class RecordingTransport(httpx.MockTransport):
    def __init__(self, responses):
        self.requests = []
        self._responses = iter(responses)
        super().__init__(self._handle)

    async def _handle(self, request):
        self.requests.append(request)
        response = next(self._responses)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def public_urls(monkeypatch):
    async def validate(url):
        if "private" in url:
            raise UnsafeUrlError("private target")
        return ("93.184.216.34",)

    monkeypatch.setattr("app.acquisition.firecrawl.ensure_public_url", validate)


async def test_firecrawl_requests_uncached_raw_html(public_urls):
    from app.acquisition.firecrawl import FirecrawlAdapter

    transport = RecordingTransport([
        httpx.Response(200, json={"success": True, "data": {
            "rawHtml": "<html><body>Example</body></html>",
            "metadata": {"sourceURL": "https://example.com/final"},
            "statusCode": 200,
        }}),
    ])
    adapter = FirecrawlAdapter("secret", transport=transport)
    result = await adapter.acquire(ProviderRequest(
        url="https://example.com", route="firecrawl_scrape",
        timeout_seconds=60, only_main_content=True,
    ))

    request = transport.requests[0]
    body = json.loads(request.content)
    assert request.url == "https://api.firecrawl.dev/v2/scrape"
    assert body["formats"] == ["rawHtml"]
    assert body["maxAge"] == 0
    assert body["storeInCache"] is False
    assert body["skipTlsVerification"] is False
    assert result.final_url == "https://example.com/final"
    assert result.native_cost.values == {"credits": 1}
    await adapter.aclose()


async def test_firecrawl_errors_are_classified_and_never_include_api_key(public_urls, caplog):
    from app.acquisition.firecrawl import FirecrawlAdapter

    transport = RecordingTransport([httpx.Response(401, text="not authorized")])
    adapter = FirecrawlAdapter("secret", transport=transport)
    with pytest.raises(ProviderFailure) as error:
        await adapter.acquire(ProviderRequest(
            url="https://example.com", route="firecrawl_scrape",
            timeout_seconds=60, only_main_content=True,
        ))
    assert error.value.code == "provider_auth"
    assert error.value.retryable is False
    assert "secret" not in caplog.text
    assert "secret" not in str(error.value)
    await adapter.aclose()


async def test_firecrawl_revalidates_provider_final_url(public_urls):
    from app.acquisition.firecrawl import FirecrawlAdapter

    transport = RecordingTransport([
        httpx.Response(200, json={"success": True, "data": {
            "rawHtml": "<html></html>",
            "metadata": {"sourceURL": "http://private.example"},
        }}),
    ])
    adapter = FirecrawlAdapter("secret", transport=transport)
    with pytest.raises(UnsafeUrlError):
        await adapter.acquire(ProviderRequest(
            url="https://example.com", route="firecrawl_scrape",
            timeout_seconds=60, only_main_content=True,
        ))
    await adapter.aclose()


@pytest.mark.parametrize("base_url", [
    "http://api.firecrawl.dev", "https://secret@api.firecrawl.dev", "https:///missing-host",
])
async def test_firecrawl_rejects_unsafe_api_urls(base_url):
    from app.acquisition.firecrawl import FirecrawlAdapter

    with pytest.raises(ValueError, match="HTTPS without credentials"):
        FirecrawlAdapter("secret", base_url=base_url)


async def test_firecrawl_rejects_bool_status_and_oversized_provider_bodies(public_urls, monkeypatch):
    from app.acquisition import firecrawl
    from app.acquisition.firecrawl import FirecrawlAdapter

    bool_status = RecordingTransport([httpx.Response(200, json={"success": True, "data": {
        "rawHtml": "<html></html>", "statusCode": True,
    }})])
    bool_adapter = FirecrawlAdapter("secret", transport=bool_status)
    with pytest.raises(ProviderFailure, match="provider_protocol_error"):
        await bool_adapter.acquire(ProviderRequest(
            url="https://example.com", route="firecrawl_scrape",
            timeout_seconds=60, only_main_content=True,
        ))
    await bool_adapter.aclose()

    monkeypatch.setattr(firecrawl, "MAX_DOM_BYTES", 64)
    oversized = RecordingTransport([httpx.Response(200, json={"success": True, "data": {
        "rawHtml": "x" * 128,
    }})])
    oversized_adapter = FirecrawlAdapter("secret", transport=oversized)
    with pytest.raises(ProviderFailure, match="response_too_large"):
        await oversized_adapter.acquire(ProviderRequest(
            url="https://example.com", route="firecrawl_scrape",
            timeout_seconds=60, only_main_content=True,
        ))
    await oversized_adapter.aclose()


async def test_firecrawl_interact_is_code_only_closes_and_discards_live_url(public_urls):
    from app.acquisition.firecrawl import FirecrawlAdapter, FirecrawlSessionBackend

    class LocalScraper:
        def _build_result(self, html, url, only_main_content, engine_used, **kwargs):
            assert html == "<html><body>after interaction</body></html>"
            assert url == "https://example.com/final"
            assert engine_used == "firecrawl"
            return {"title": "Local", "markdown": "local extraction", "metadata": {}}

    transport = RecordingTransport([
        httpx.Response(200, json={"success": True, "data": {
            "id": "remote-1", "liveViewUrl": "https://secret-live.example/token",
        }}),
        httpx.Response(200, json={"success": True, "data": {}}),
        httpx.Response(200, json={"success": True, "data": {
            "rawHtml": "<html><body>after interaction</body></html>",
            "metadata": {"sourceURL": "https://example.com/final"},
            "statusCode": 200,
        }}),
        httpx.Response(200, json={"success": True, "data": {"creditsBilled": 1}}),
    ])
    adapter = FirecrawlAdapter("secret", transport=transport)
    backend = FirecrawlSessionBackend(adapter, timeout_seconds=60, scraper=LocalScraper())
    handle = SessionHandle(
        id=uuid4(), backend="firecrawl", expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    snapshot = await backend.create(handle, "https://example.com", None)
    assert snapshot.usage == {"credits": 2}
    assert "secret-live" not in repr(snapshot)
    await backend.send(handle, {"code": "await page.waitForTimeout(1)"})
    result = await backend.resume(handle)

    assert result.markdown == "local extraction"
    assert backend.native_cost(handle).values == {"credits": 1}
    assert backend.native_cost(handle).estimated is False
    assert [request.method for request in transport.requests] == ["POST", "POST", "GET", "DELETE"]
    interact = json.loads(transport.requests[1].content)
    assert interact == {"code": "await page.waitForTimeout(1)"}
    assert all("prompt" not in request.content.decode() for request in transport.requests)
    await adapter.aclose()


async def test_firecrawl_interact_closes_after_a_terminal_failure(public_urls):
    from app.acquisition.firecrawl import FirecrawlAdapter, FirecrawlSessionBackend

    transport = RecordingTransport([
        httpx.Response(200, json={"success": True, "data": {"id": "remote-1"}}),
        httpx.Response(500, json={"success": False}),
        httpx.Response(204),
    ])
    adapter = FirecrawlAdapter("secret", transport=transport)
    backend = FirecrawlSessionBackend(adapter, timeout_seconds=60)
    handle = SessionHandle(
        id=uuid4(), backend="firecrawl", expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    await backend.create(handle, "https://example.com", None)
    with pytest.raises(ProviderFailure) as error:
        await backend.send(handle, {"code": "await page.waitForTimeout(1)"})
    assert error.value.retryable is True
    assert [request.method for request in transport.requests] == ["POST", "POST", "DELETE"]
    await adapter.aclose()


async def test_firecrawl_rejects_billed_credits_above_the_reservation(public_urls):
    from app.acquisition.firecrawl import FirecrawlAdapter, FirecrawlSessionBackend

    transport = RecordingTransport([
        httpx.Response(200, json={"success": True, "data": {"id": "remote-1"}}),
        httpx.Response(200, json={"success": True, "data": {"creditsBilled": 3}}),
    ])
    adapter = FirecrawlAdapter("secret", transport=transport)
    backend = FirecrawlSessionBackend(adapter, timeout_seconds=60)
    handle = SessionHandle(
        id=uuid4(), backend="firecrawl", expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    await backend.create(handle, "https://example.com", None)
    with pytest.raises(ProviderFailure, match="provider_protocol_error"):
        await backend.close(handle)
    assert [request.method for request in transport.requests] == ["POST", "DELETE"]
    await adapter.aclose()
