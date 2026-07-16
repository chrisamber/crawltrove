"""POST /api/map — fast URL discovery (sitemap + shallow link pass), no scraping.

Hermetic: sitemap fetches and the base-page fetch are mocked; the API tests run
through the real FastAPI app over an ASGI transport.
"""
import httpx

from app import sitemap


def _fake_fetch(html_by_url):
    async def fake(url, timeout_s=20):
        if url in html_by_url:
            return {"status": 200, "html": html_by_url[url], "content": b"",
                    "final_url": url, "content_type": "text/html"}
        return {"status": 404, "html": "", "content": b"",
                "final_url": url, "content_type": "text/html"}
    return fake


def _client():
    from app.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")


async def test_map_merges_sitemap_and_page_links(monkeypatch):
    async def fake_discover(base_url, cap=200):
        return ["https://site.test/docs", "https://site.test/blog"]

    html = ('<a href="/pricing">p</a>'
            '<a href="https://site.test/docs/">dup of sitemap</a>'
            '<a href="https://other.test/x">offsite</a>')
    monkeypatch.setattr(sitemap, "discover", fake_discover)
    monkeypatch.setattr(sitemap.fetch, "fetch_http",
                        _fake_fetch({"https://site.test": html}))

    links = await sitemap.map_site("https://site.test")
    assert "https://site.test" in links
    assert "https://site.test/docs" in links
    assert "https://site.test/blog" in links
    assert "https://site.test/pricing" in links
    assert all("other.test" not in u for u in links)
    # /docs/ from the page dedups against /docs from the sitemap
    assert len([u for u in links if "docs" in u]) == 1


async def test_map_search_filter_and_limit(monkeypatch):
    async def fake_discover(base_url, cap=200):
        return [f"https://site.test/docs/page-{i}" for i in range(10)] + [
            "https://site.test/blog/post"]

    monkeypatch.setattr(sitemap, "discover", fake_discover)
    monkeypatch.setattr(sitemap.fetch, "fetch_http", _fake_fetch({}))

    links = await sitemap.map_site("https://site.test", search="DOCS", limit=3)
    assert len(links) == 3
    assert all("docs" in u for u in links)


async def test_map_sitemap_only_skips_page_fetch(monkeypatch):
    async def fake_discover(base_url, cap=200):
        return ["https://site.test/a"]

    async def exploding_fetch(url, timeout_s=20):
        raise AssertionError("fetch_http must not be called with sitemapOnly")

    monkeypatch.setattr(sitemap, "discover", fake_discover)
    monkeypatch.setattr(sitemap.fetch, "fetch_http", exploding_fetch)

    links = await sitemap.map_site("https://site.test", sitemap_only=True)
    assert links == ["https://site.test", "https://site.test/a"]


async def test_map_page_fetch_failure_degrades(monkeypatch):
    async def fake_discover(base_url, cap=200):
        return ["https://site.test/a"]

    async def broken_fetch(url, timeout_s=20):
        raise RuntimeError("network down")

    monkeypatch.setattr(sitemap, "discover", fake_discover)
    monkeypatch.setattr(sitemap.fetch, "fetch_http", broken_fetch)

    links = await sitemap.map_site("https://site.test")
    assert links == ["https://site.test", "https://site.test/a"]


async def test_map_endpoint(monkeypatch):
    async def fake_map_site(url, *, limit, search, sitemap_only):
        assert (url, limit, search, sitemap_only) == (
            "https://site.test", 5, "doc", True)
        return ["https://site.test/docs"]

    monkeypatch.setattr(sitemap, "map_site", fake_map_site)
    async with _client() as c:
        resp = await c.post("/api/map", json={
            "url": "https://site.test", "limit": 5,
            "search": "doc", "sitemapOnly": True})
    assert resp.status_code == 200
    assert resp.json() == {"success": True,
                           "links": ["https://site.test/docs"], "count": 1}


async def test_map_endpoint_rejects_non_http(monkeypatch):
    async with _client() as c:
        resp = await c.post("/api/map", json={"url": "ftp://site.test"})
    assert resp.status_code == 400
