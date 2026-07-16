"""POST /api/search/web — thin wrapper over the search provider waterfall."""
import httpx

from app import search


def _client():
    from app.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")


async def test_search_web_shape(monkeypatch):
    async def fake_search(query, n=8):
        assert (query, n) == ("swift actors", 5)
        return [{"url": f"https://ex.com/{i}", "title": f"t{i}", "snippet": "s"}
                for i in range(3)]

    monkeypatch.setattr(search, "search", fake_search)
    monkeypatch.setattr(search, "provider", lambda: "ddg")
    async with _client() as c:
        resp = await c.post("/api/search/web",
                            json={"query": "swift actors", "limit": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["provider"] == "ddg"
    assert body["count"] == 3
    assert body["results"][0] == {"url": "https://ex.com/0", "title": "t0",
                                  "snippet": "s"}


async def test_search_web_provider_failure_is_empty_not_5xx(monkeypatch):
    async def broken(query, n=8):
        return []          # search.search never raises; [] is the failure mode

    monkeypatch.setattr(search, "search", broken)
    async with _client() as c:
        resp = await c.post("/api/search/web", json={"query": "anything"})
    assert resp.status_code == 200
    assert resp.json() == {"success": True, "provider": search.provider(),
                           "results": [], "count": 0}


async def test_search_web_validation():
    async with _client() as c:
        assert (await c.post("/api/search/web",
                             json={"query": ""})).status_code == 422
        assert (await c.post("/api/search/web",
                             json={"query": "x", "limit": 50})).status_code == 422
