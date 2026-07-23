"""LocalAdapter raw-capture path (no full scrape → strip → rebuild)."""
from types import SimpleNamespace

import pytest

from app.acquisition.local import LocalAdapter
from app.acquisition.providers import NativeCost, ProviderFailure, ProviderRequest


def _request(route="local_http", **kwargs):
    return ProviderRequest(
        url="https://example.com/page",
        route=route,
        timeout_seconds=60,
        only_main_content=True,
        max_decoded_bytes=1024,
        **kwargs,
    )


async def test_local_http_returns_raw_html_with_downloaded_bytes(monkeypatch):
    async def fetch_http(url, **kwargs):
        assert kwargs.get("max_decoded_bytes") == 1024
        return {
            "status": 200,
            "html": "<html><body><p>hello</p></body></html>",
            "content": b"hello-bytes",
            "content_type": "text/html",
            "final_url": url,
        }

    monkeypatch.setattr("app.acquisition.local.fetch.fetch_http", fetch_http)
    monkeypatch.setattr("app.acquisition.local.documents.sniff", lambda *a, **k: None)
    monkeypatch.setattr("app.acquisition.local.fetch.is_challenge_html", lambda html: False)

    adapter = LocalAdapter(SimpleNamespace())
    result = await adapter.acquire(_request())
    assert result.raw_html.startswith("<html>")
    assert result.downloaded_bytes == len(b"hello-bytes")
    assert result.prebuilt_markdown is None
    assert result.status_code == 200


async def test_local_http_maps_challenge_to_provider_failure(monkeypatch):
    async def fetch_http(url, **kwargs):
        return {
            "status": 403,
            "html": "Just a moment... checking your browser",
            "content": b"x",
            "content_type": "text/html",
            "final_url": url,
        }

    monkeypatch.setattr("app.acquisition.local.fetch.fetch_http", fetch_http)
    # Non-2xx fails before challenge check for 403
    adapter = LocalAdapter(SimpleNamespace())
    with pytest.raises(ProviderFailure) as exc:
        await adapter.acquire(_request())
    assert exc.value.code == "http_status_error"


async def test_local_http_challenge_on_200(monkeypatch):
    async def fetch_http(url, **kwargs):
        return {
            "status": 200,
            "html": "Just a moment... checking your browser",
            "content": b"x",
            "content_type": "text/html",
            "final_url": url,
        }

    monkeypatch.setattr("app.acquisition.local.fetch.fetch_http", fetch_http)
    monkeypatch.setattr("app.acquisition.local.documents.sniff", lambda *a, **k: None)
    monkeypatch.setattr("app.acquisition.local.fetch.is_challenge_html", lambda html: True)

    with pytest.raises(ProviderFailure) as exc:
        await LocalAdapter(SimpleNamespace()).acquire(_request())
    assert exc.value.code == "blocked_challenge"
    assert exc.value.retryable is True


async def test_local_browser_uses_render_and_before_browser(monkeypatch):
    called = {}

    async def public(url):
        return None

    class Browser:
        async def render(self, url, **kwargs):
            called.update(kwargs)
            return {
                "html": "<p>rendered</p>",
                "final_url": url,
                "status_code": 200,
                "screenshot": b"shot",
                "blocked_challenge": False,
            }

    async def before():
        called["before"] = True
        return True

    monkeypatch.setattr("app.acquisition.local.ensure_public_url", public)
    monkeypatch.setattr("app.acquisition.local.fetch.is_challenge_html", lambda html: False)

    adapter = LocalAdapter(SimpleNamespace(browser=Browser()))
    result = await adapter.acquire(_request(
        route="local_browser",
        capture_screenshot=True,
        before_browser=before,
    ))
    assert result.raw_html == "<p>rendered</p>"
    assert result.screenshot == b"shot"
    assert called["capture_screenshot"] is True
    assert called["before"] is True


async def test_local_browser_budget_gate():
    async def deny():
        return False

    adapter = LocalAdapter(SimpleNamespace(browser=object()))
    with pytest.raises(ProviderFailure) as exc:
        await adapter.acquire(_request(route="local_browser", before_browser=deny))
    assert exc.value.code == "browser_budget_exhausted"
