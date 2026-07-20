"""Tests for the service-side corpus browser.

Reads data/corpus/**/*.jsonl generically. Hermetic: a tmp corpus tree is built
and CORPUS_DIR is pointed at it; the API is driven over ASGI (no network).
"""
import ast
import json
import pathlib

import httpx
import pytest

from app import corpus_browser


def _write(root, target, namespace, framework, records):
    d = root / target / namespace
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f"{framework}.jsonl", "a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _rec(**kw):
    base = {"id": "h", "url": "https://x/y", "title": "T", "text": "body text",
            "namespace": "apple-framework", "framework": "swiftui",
            "license_bucket": "apple-developer-docs-review-required",
            "quality_tier": "high", "chunk_index": 0, "heading_path": ["T"]}
    base.update(kw)
    return base


@pytest.fixture
def tmp_corpus(tmp_path, monkeypatch):
    root = tmp_path / "corpus"
    monkeypatch.setattr(corpus_browser, "CORPUS_DIR", str(root))
    corpus_browser._stats_cache["sig"] = None
    corpus_browser._stats_cache["value"] = None
    _write(root, "rag", "apple-framework", "swiftui", [
        _rec(id="a", title="View", text="A view container", chunk_index=0),
        _rec(id="b", title="Text", text="A text label", chunk_index=1, quality_tier="medium"),
    ])
    _write(root, "rag", "swift-language", "general", [
        _rec(id="c", title="Actors", text="Concurrency actors",
             namespace="swift-language", framework="", license_bucket="cc-by-4.0",
             quality_tier="high"),
    ])
    _write(root, "dapt", "swift-language", "general", [
        _rec(id="d", title="Actors DAPT", text="whole page text",
             namespace="swift-language", framework="", license_bucket="cc-by-4.0"),
    ])
    return root


def test_browse_returns_all(tmp_corpus):
    page = corpus_browser.browse()
    assert page["count"] == 4
    assert {i["id"] for i in page["items"]} == {"a", "b", "c", "d"}
    assert page["hasMore"] is False


def test_filter_by_namespace(tmp_corpus):
    page = corpus_browser.browse(namespace="swift-language")
    assert {i["id"] for i in page["items"]} == {"c", "d"}


def test_filter_by_target_and_tier(tmp_corpus):
    page = corpus_browser.browse(target="rag", tier="medium")
    assert [i["id"] for i in page["items"]] == ["b"]


def test_filter_by_bucket(tmp_corpus):
    page = corpus_browser.browse(bucket="cc-by-4.0")
    assert {i["id"] for i in page["items"]} == {"c", "d"}


def test_query_substring(tmp_corpus):
    page = corpus_browser.browse(q="concurrency")
    assert [i["id"] for i in page["items"]] == ["c"]


def test_pagination_and_has_more(tmp_corpus):
    page1 = corpus_browser.browse(limit=2, offset=0)
    assert page1["count"] == 2
    assert page1["hasMore"] is True
    page2 = corpus_browser.browse(limit=2, offset=2)
    assert page2["count"] == 2
    assert page2["hasMore"] is False
    ids = {i["id"] for i in page1["items"]} | {i["id"] for i in page2["items"]}
    assert ids == {"a", "b", "c", "d"}


def test_stats_counts_and_cache(tmp_corpus):
    st = corpus_browser.stats()
    assert st["total"] == 4
    assert st["byTarget"] == {"rag": 3, "dapt": 1}
    assert st["byTier"]["high"] == 3
    assert st["byTier"]["medium"] == 1
    assert "swift-language" in st["namespaces"]
    assert "swiftui" in st["frameworks"]
    # cache returns the same object until the tree changes
    assert corpus_browser.stats() is st


def test_empty_corpus_is_empty_not_error(tmp_path, monkeypatch):
    monkeypatch.setattr(corpus_browser, "CORPUS_DIR", str(tmp_path / "nope"))
    corpus_browser._stats_cache["sig"] = None
    assert corpus_browser.browse()["items"] == []
    assert corpus_browser.stats()["total"] == 0


# --- API + invariant -------------------------------------------------------
def _client():
    from app.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")


async def test_api_corpus_and_stats(tmp_corpus):
    async with _client() as c:
        r1 = await c.get("/api/corpus", params={"namespace": "swift-language"})
        r2 = await c.get("/api/corpus/stats")
        r3 = await c.get("/api/corpus", params={"target": "bogus"})
    assert r1.status_code == 200
    assert {i["id"] for i in r1.json()["items"]} == {"c", "d"}
    assert r2.status_code == 200 and r2.json()["stats"]["total"] == 4
    assert r3.status_code == 400


def test_browser_does_not_import_corpus_pipeline():
    # One-way dependency: the service-side browser must not import app.corpus.
    src = pathlib.Path(corpus_browser.__file__).read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("app.corpus")
        if isinstance(node, ast.Import):
            assert all(not a.name.startswith("app.corpus") for a in node.names)
