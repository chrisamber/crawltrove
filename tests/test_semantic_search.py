"""API tests for GET /api/search/semantic.

Hermetic: embeddings + vecindex are mocked, so no network and no real index.
Exercises the 501-when-unconfigured contract, argument validation, and the
artifact-path enrichment on hits.
"""
import httpx
import pytest

from app import embeddings, retrieval, vecindex


def _client():
    from app.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")


async def test_501_when_no_backend(monkeypatch):
    monkeypatch.setattr(embeddings, "configured", lambda: False)
    async with _client() as c:
        resp = await c.get("/api/search/semantic", params={"q": "hello"})
    assert resp.status_code == 501


async def test_empty_query_is_400(monkeypatch):
    monkeypatch.setattr(embeddings, "configured", lambda: True)
    async with _client() as c:
        resp = await c.get("/api/search/semantic", params={"q": "   "})
    assert resp.status_code == 400


async def test_bad_kind_is_400(monkeypatch):
    monkeypatch.setattr(embeddings, "configured", lambda: True)
    async with _client() as c:
        resp = await c.get("/api/search/semantic", params={"q": "x", "kind": "bogus"})
    assert resp.status_code == 400


async def test_results_are_enriched_with_paths(monkeypatch):
    monkeypatch.setattr(embeddings, "configured", lambda: True)

    async def fake_search(*args, **kwargs):
        return [
            {"kind": "scrape", "ref": "STEM1", "url": "http://a",
             "chunkIndex": 0, "snippet": "hello", "distance": 0.1,
             "score": 0.9, "meta": {"title": "A"}},
            {"kind": "crawl", "ref": "STEM2#3", "url": "http://b",
             "chunkIndex": 3, "snippet": "world", "distance": 0.2,
             "score": 0.8, "meta": {"title": "B"}},
            {"kind": "corpus", "ref": "abc123", "url": "http://c",
             "chunkIndex": 0, "snippet": "corp", "distance": 0.3,
             "score": 0.7, "meta": {"file": "corpus/rag/ns/fw.jsonl"}},
        ]

    monkeypatch.setattr(retrieval, "search", fake_search)
    async with _client() as c:
        resp = await c.get("/api/search/semantic", params={"q": "hi", "k": "5"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 3
    r = body["results"]
    assert r[0]["json"] == "/data/scrapes/STEM1.json"
    assert r[0]["md"] == "/data/scrapes/STEM1.md"
    # Crawl ref carries a #pageIndex suffix that resolves to the crawl artifact.
    assert r[1]["json"] == "/data/crawls/STEM2.json"
    # Corpus path comes from meta.file (records share a JSONL file).
    assert r[2]["json"] == "/data/corpus/rag/ns/fw.jsonl"
    assert r[2]["md"] is None


async def test_backend_down_midrequest_returns_empty(monkeypatch):
    monkeypatch.setattr(embeddings, "configured", lambda: True)

    async def none_embed(q):
        return None

    monkeypatch.setattr(embeddings, "embed_query", none_embed)
    monkeypatch.setattr(vecindex, "available", lambda: True)
    async with _client() as c:
        resp = await c.get("/api/search/semantic", params={"q": "hi"})
    assert resp.status_code == 200
    assert resp.json()["results"] == []


async def test_semantic_forwards_corpus_filters(monkeypatch):
    monkeypatch.setattr(embeddings, "configured", lambda: True)

    captured = {}

    async def fake_search(*args, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(retrieval, "search", fake_search)
    async with _client() as c:
        resp = await c.get("/api/search/semantic", params={
            "q": "actors", "kind": "corpus", "namespace": "swift-language",
            "tier": "high",
        })
    assert resp.status_code == 200
    assert captured["filters"] == {
        "namespace": "swift-language", "tier": "high"}
    assert captured["mode"] == "semantic"
    assert resp.json()["filters"]["framework"] is None


async def test_stats_endpoint(monkeypatch):
    monkeypatch.setattr(embeddings, "configured", lambda: True)
    monkeypatch.setattr(vecindex, "stats",
                        lambda: {"available": True, "total": 5,
                                 "byKind": {"scrape": 5}, "model": "m", "dim": 3})
    async with _client() as c:
        resp = await c.get("/api/search/semantic/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True
    assert body["index"]["total"] == 5
