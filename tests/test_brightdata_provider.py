import json

import httpx
import pytest

from app.acquisition.providers import ProviderFailure, ProviderRequest
from app.url_safety import UnsafeUrlError


def _request() -> ProviderRequest:
    return ProviderRequest(
        url="https://example.com", route="brightdata_unlocker",
        timeout_seconds=60, only_main_content=True,
    )


@pytest.mark.parametrize("api_url", [
    "http://api.brightdata.com/request",
    "https://secret@api.brightdata.com/request",
    "https:///request",
])
def test_brightdata_rejects_unsafe_api_urls(api_url):
    from app.acquisition.brightdata import BrightDataAdapter

    with pytest.raises(ValueError, match="HTTPS without credentials"):
        BrightDataAdapter("secret", "zone-a", api_url=api_url)


@pytest.fixture
def mock_transport():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text="<html><body>ok</body></html>")

    transport = httpx.MockTransport(handler)
    transport.requests = requests
    return transport


@pytest.fixture(autouse=True)
def public_urls(monkeypatch):
    async def validate(url):
        if "private" in url:
            raise UnsafeUrlError("private target")
        return ("93.184.216.34",)

    monkeypatch.setattr("app.acquisition.brightdata.ensure_public_url", validate)


async def test_brightdata_uses_direct_unlocker_api(mock_transport, monkeypatch):
    from app.acquisition import brightdata
    from app.acquisition.brightdata import BrightDataAdapter

    validated = []
    client_kwargs = {}
    original_client = brightdata.httpx.AsyncClient

    class CapturingClient(original_client):
        def __init__(self, *args, **kwargs):
            client_kwargs.update(kwargs)
            super().__init__(*args, **kwargs)

    async def public(url):
        validated.append(url)

    monkeypatch.setattr(brightdata, "ensure_public_url", public)
    monkeypatch.setattr(brightdata.httpx, "AsyncClient", CapturingClient)
    adapter = BrightDataAdapter("secret", "zone-a", transport=mock_transport)
    result = await adapter.acquire(_request())
    request = mock_transport.requests[0]
    assert str(request.url) == "https://api.brightdata.com/request"
    assert json.loads(request.content) == {
        "zone": "zone-a", "url": "https://example.com", "format": "raw",
    }
    assert request.headers["authorization"] == "Bearer secret"
    assert result.raw_html.startswith("<html")
    assert result.final_url == "https://example.com"
    assert result.native_cost.values == {"requests": 1}
    assert validated == ["https://example.com", "https://example.com"]
    assert client_kwargs["trust_env"] is False


async def test_brightdata_revalidates_reported_final_url(mock_transport, monkeypatch):
    from app.acquisition import brightdata
    from app.acquisition.brightdata import BrightDataAdapter

    mock_transport = httpx.MockTransport(lambda request: httpx.Response(
        200, json={"body": "<html>ok</html>", "url": "https://final.example/page"},
    ))
    validated = []

    async def public(url):
        validated.append(url)

    monkeypatch.setattr(brightdata, "ensure_public_url", public)
    result = await BrightDataAdapter("secret", "zone-a", transport=mock_transport).acquire(_request())
    assert result.final_url == "https://final.example/page"
    assert validated == ["https://example.com", "https://final.example/page"]


async def test_brightdata_preserves_raw_json_target_content():
    from app.acquisition.brightdata import BrightDataAdapter

    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, content=b'{"title":"a target JSON document"}',
    ))
    result = await BrightDataAdapter("secret", "zone-a", transport=transport).acquire(_request())
    assert result.raw_html == '{"title":"a target JSON document"}'
    assert result.final_url == "https://example.com"


async def test_brightdata_rejects_unsafe_initial_url_before_a_billable_call():
    from app.acquisition.brightdata import BrightDataAdapter

    requests = []
    transport = httpx.MockTransport(lambda request: requests.append(request))
    with pytest.raises(ProviderFailure) as error:
        await BrightDataAdapter("secret", "zone-a", transport=transport).acquire(
            ProviderRequest(
                url="https://private.example", route="brightdata_unlocker",
                timeout_seconds=60, only_main_content=True,
            )
        )
    assert error.value.code == "unsafe_request_url"
    assert error.value.retryable is False
    assert error.value.native_cost.values == {"requests": 0}
    assert requests == []


@pytest.mark.parametrize(("status_code", "code", "retryable"), [
    (401, "provider_auth", False),
    (402, "provider_billing", False),
    (429, "provider_rate_limited", True),
    (500, "provider_failure", True),
    (400, "provider_request", False),
])
async def test_brightdata_classifies_non_success_without_secrets(
    status_code, code, retryable, caplog
):
    from app.acquisition.brightdata import BrightDataAdapter

    transport = httpx.MockTransport(lambda request: httpx.Response(
        status_code, headers={"x-brd-error": "secret", "retry-after": "30"}, text="secret"
    ))
    with pytest.raises(ProviderFailure) as error:
        await BrightDataAdapter("secret", "zone-a", transport=transport).acquire(_request())
    assert error.value.code == code
    assert error.value.retryable is retryable
    assert error.value.native_cost.values == {"requests": 1}
    assert error.value.retry_after_seconds == (30 if status_code == 429 else None)
    assert "secret" not in caplog.text


@pytest.mark.parametrize(("header", "expected"), [
    ("0", 0),
    ("9999999999", 60 * 60),
    ("Wed, 21 Oct 2099 07:28:00 GMT", 60 * 60),
    ("Thu, 01 Jan 1970 00:00:00 GMT", 0),
    ("tomorrow", None),
    ("-1", None),
])
async def test_brightdata_normalizes_retry_after(header, expected):
    from app.acquisition.brightdata import BrightDataAdapter

    transport = httpx.MockTransport(lambda request: httpx.Response(
        429, headers={"retry-after": header},
    ))
    with pytest.raises(ProviderFailure) as error:
        await BrightDataAdapter("secret", "zone-a", transport=transport).acquire(_request())
    assert error.value.retry_after_seconds == expected


async def test_brightdata_rejects_challenges_and_oversize_bodies(monkeypatch):
    from app.acquisition import brightdata
    from app.acquisition.brightdata import BrightDataAdapter, MAX_HTML_BYTES

    async def public(url):
        return None

    monkeypatch.setattr(brightdata, "ensure_public_url", public)
    challenge = httpx.MockTransport(lambda request: httpx.Response(
        200, text="<html><iframe src='https://captcha.example'></iframe></html>"
    ))
    with pytest.raises(ProviderFailure, match="blocked_challenge"):
        await BrightDataAdapter("secret", "zone-a", transport=challenge).acquire(_request())
    oversize = httpx.MockTransport(lambda request: httpx.Response(
        200, content=b"x" * (MAX_HTML_BYTES + 1)
    ))
    with pytest.raises(ProviderFailure, match="response_too_large"):
        await BrightDataAdapter("secret", "zone-a", transport=oversize).acquire(_request())


def test_brightdata_redacts_auth_and_billing_headers():
    from app.acquisition.brightdata import redact_headers

    assert redact_headers({
        "authorization": "Bearer secret", "x-brd-cost": "7", "content-type": "text/html",
    }) == {"content-type": "text/html"}
