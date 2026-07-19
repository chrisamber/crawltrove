import httpx
import pytest

from crawltrove_mcp.client import CrawlTroveClient, CrawlTroveError


def test_api_key_is_sent_as_request_header(monkeypatch):
    monkeypatch.setenv("CRAWLTROVE_API_KEY", "test-key")
    captured = {}

    def handler(request):
        captured["api_key"] = request.headers.get("x-api-key")
        return httpx.Response(200, json={"results": [], "count": 0})

    client = CrawlTroveClient(
        base_url="http://service", transport=httpx.MockTransport(handler))

    client.search("actors")

    assert captured["api_key"] == "test-key"


def test_search_forwards_filters_to_hybrid_endpoint():
    captured = {}

    def handler(request):
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"results": [], "count": 0})

    client = CrawlTroveClient(
        base_url="http://service", transport=httpx.MockTransport(handler))

    assert client.search(
        "actors", kind="corpus", mode="keyword", namespace="swift-language",
        tier="high", k=5,
    ) == {"results": [], "count": 0}
    assert captured == {
        "path": "/api/search/hybrid",
        "params": {
            "q": "actors", "k": "5", "mode": "keyword", "kind": "corpus",
            "namespace": "swift-language", "tier": "high",
        },
    }


def test_nonobject_http_error_is_typed():
    client = CrawlTroveClient(
        base_url="http://service",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(502, json=["bad gateway"])),
    )

    with pytest.raises(CrawlTroveError) as error:
        client.scrape("https://example.com")

    assert error.value.kind == "upstream_failed"
    assert error.value.status == 502
