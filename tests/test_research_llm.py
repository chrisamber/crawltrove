"""Research LLM step tests — extract_llm is mocked; these verify wiring,
retry behaviour, and post-filtering, not model output quality."""
import pytest

from app import extract_llm, research_llm


def _fake_extract(data):
    async def fake(markdown, url, schema, prompt="", **kw):
        return {"data": data, "model": "fake", "usage": {}}
    return fake


async def test_plan_queries_drops_blanks_and_caps(monkeypatch):
    monkeypatch.setattr(extract_llm, "extract", _fake_extract(
        {"queries": ["q1", "  ", "q2", "q3", "q4", "q5"]}))
    out = await research_llm.plan_queries("what is x?", max_queries=4)
    assert out == ["q1", "q2", "q3", "q4"]


async def test_select_urls_filters_to_candidates(monkeypatch):
    monkeypatch.setattr(extract_llm, "extract", _fake_extract(
        {"urls": ["https://a.example", "https://hallucinated.example",
                  "https://b.example"]}))
    candidates = [{"url": "https://a.example", "title": "A", "snippet": ""},
                  {"url": "https://b.example", "title": "B", "snippet": ""}]
    out = await research_llm.select_urls("q", candidates, k=2)
    assert out == ["https://a.example", "https://b.example"]


async def test_retry_then_success(monkeypatch):
    monkeypatch.setattr(research_llm, "BACKOFF_BASE", 0)
    calls = {"n": 0}

    async def flaky(markdown, url, schema, prompt="", **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("backend hiccup")
        return {"data": {"queries": ["ok"]}, "model": "fake", "usage": {}}

    monkeypatch.setattr(extract_llm, "extract", flaky)
    assert await research_llm.plan_queries("q") == ["ok"]
    assert calls["n"] == 2


async def test_retry_exhausted_raises(monkeypatch):
    monkeypatch.setattr(research_llm, "BACKOFF_BASE", 0)

    async def dead(markdown, url, schema, prompt="", **kw):
        raise RuntimeError("backend down")

    monkeypatch.setattr(extract_llm, "extract", dead)
    with pytest.raises(RuntimeError, match="backend down"):
        await research_llm.plan_queries("q")


def test_validate_citations():
    assert research_llm.validate_citations("Claims [1] and [3].", 2) == [3]
    assert research_llm.validate_citations("no cites here", 2) == []
    assert research_llm.validate_citations("[0] is invalid too", 2) == [0]
