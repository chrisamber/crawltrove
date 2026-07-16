"""Search provider unit tests. Hermetic: parsers are pure; the one
connection-attempting test points at a closed local port."""
from app import search

SEARX_BODY = {"results": [
    {"url": "https://a.example/1", "title": "A", "content": "first"},
    {"url": "https://b.example/2", "title": "B", "content": "second"},
    {"title": "no url — skipped"},
]}


def test_parse_searxng():
    assert search.parse_searxng(SEARX_BODY, 5) == [
        {"url": "https://a.example/1", "title": "A", "snippet": "first"},
        {"url": "https://b.example/2", "title": "B", "snippet": "second"},
    ]


def test_parse_searxng_caps_n():
    assert len(search.parse_searxng(SEARX_BODY, 1)) == 1


BRAVE_BODY = {"web": {"results": [
    {"url": "https://c.example", "title": "C", "description": "third"}]}}


def test_parse_brave():
    assert search.parse_brave(BRAVE_BODY, 5) == [
        {"url": "https://c.example", "title": "C", "snippet": "third"}]


DDG_HTML = """
<div class="result__body">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fd.example%2Fpage&rut=x">D page</a>
  <a class="result__snippet" href="#">a snippet here</a>
</div>
"""


def test_parse_ddg_unwraps_redirect():
    assert search.parse_ddg(DDG_HTML, 5) == [
        {"url": "https://d.example/page", "title": "D page",
         "snippet": "a snippet here"}]


def test_provider_selection(monkeypatch):
    monkeypatch.delenv("SEARXNG_BASE_URL", raising=False)
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    assert search.provider() == "ddg"
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "k")
    assert search.provider() == "brave"
    monkeypatch.setenv("SEARXNG_BASE_URL", "http://127.0.0.1:1")
    assert search.provider() == "searxng"


async def test_search_failure_returns_empty(monkeypatch):
    monkeypatch.setenv("SEARXNG_BASE_URL", "http://127.0.0.1:1")  # nothing listens
    assert await search.search("anything") == []
