import httpx
from pathlib import Path

from app import retrieval


def _client():
    from app.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")


async def test_hybrid_validation():
    async with _client() as client:
        assert (await client.get("/api/search/hybrid", params={"q": " "})).status_code == 400
        assert (await client.get("/api/search/hybrid", params={"q": "x", "kind": "bad"})).status_code == 400
        assert (await client.get("/api/search/hybrid", params={"q": "x", "mode": "bad"})).status_code == 400
        assert (await client.get("/api/search/hybrid", params={"q": "x", "k": 0})).status_code == 400


async def test_hybrid_adds_paths_ranks_and_keeps_db_paths_null(monkeypatch):
    async def fake_search(*args, **kwargs):
        return [
            {"kind": "scrape", "ref": "stem", "url": "https://a",
             "chunkIndex": 0, "snippet": "a", "score": .1,
             "semanticRank": 1, "semanticScore": .9, "meta": {}},
            {"kind": "scrape", "ref": "db:7", "url": "https://b",
             "chunkIndex": 0, "snippet": "b", "score": .08,
             "keywordRank": 1, "keywordScore": .8, "meta": {}},
        ]

    monkeypatch.setattr(retrieval, "search", fake_search)
    async with _client() as client:
        response = await client.get("/api/search/hybrid", params={"q": "term"})
    assert response.status_code == 200
    first, db = response.json()["results"]
    assert first["json"] == "/data/scrapes/stem.json"
    assert first["semanticRank"] == 1
    assert db["json"] is None and db["md"] is None


async def test_hybrid_501_only_when_selected_mode_unavailable(monkeypatch):
    async def unavailable(*args, **kwargs):
        raise retrieval.RetrievalUnavailable("none")

    monkeypatch.setattr(retrieval, "search", unavailable)
    async with _client() as client:
        response = await client.get(
            "/api/search/hybrid", params={"q": "term", "mode": "keyword"})
    assert response.status_code == 501


async def test_hybrid_forwards_and_echoes_filters(monkeypatch):
    captured = {}

    async def fake_search(*args, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(retrieval, "search", fake_search)
    params = {
        "q": "actors", "kind": "corpus", "namespace": "swift-language",
        "bucket": "cc-by", "tier": "high", "framework": "swiftui",
    }
    async with _client() as client:
        response = await client.get("/api/search/hybrid", params=params)
    assert response.status_code == 200
    assert captured["filters"] == {
        "namespace": "swift-language", "bucket": "cc-by",
        "tier": "high", "framework": "swiftui",
    }
    assert response.json()["filters"] == {"kind": "corpus", **captured["filters"]}


async def test_metadata_filters_reject_non_corpus_kind():
    async with _client() as client:
        response = await client.get(
            "/api/search/hybrid",
            params={"q": "actors", "kind": "scrape", "tier": "high"})
    assert response.status_code == 400


async def test_facets_endpoint_returns_candidate_contract(monkeypatch):
    async def fake_facets(*args, **kwargs):
        return {
            "facets": {"kind": {"corpus": 2}}, "candidateCount": 2,
            "candidateLimit": 200, "truncated": False,
        }

    monkeypatch.setattr(retrieval, "facets", fake_facets)
    async with _client() as client:
        response = await client.get(
            "/api/search/facets", params={"q": "actors", "mode": "keyword"})
    assert response.status_code == 200
    assert response.json()["candidateCount"] == 2
    assert response.json()["facets"] == {"kind": {"corpus": 2}}


def test_library_dashboard_uses_hybrid_filters_and_facets():
    root = Path(__file__).parents[1]
    html = (root / "app/static/index.html").read_text(encoding="utf-8")
    js = (root / "app/static/semantic.js").read_text(encoding="utf-8")
    for control in (
        "semanticMode", "semanticKind", "semanticNamespace",
        "semanticBucket", "semanticTier", "semanticFramework",
    ):
        assert f'id="{control}"' in html
    assert "/api/search/hybrid?" in js
    assert "/api/search/facets?" in js
    assert "facetParams.set('kind', kind)" in js
    assert "kindSelect.value !== 'corpus'" in js
    assert "matchedChunkCount" in js
