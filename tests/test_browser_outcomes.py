from types import SimpleNamespace

import pytest

from app import fetch
from app import scraper as scraper_mod
from app.scraper import WebScraper


def _http_response(status=200):
    return {
        "status": status,
        "html": "<html><body>content</body></html>",
        "content": b"content",
        "content_type": "text/html",
    }


@pytest.mark.parametrize("engine", ["auto", "http"])
async def test_transport_failure_is_not_escalated_to_browser(monkeypatch, engine):
    async def no_response(url):
        return None

    monkeypatch.setattr(fetch, "fetch_http", no_response)
    monkeypatch.setattr(scraper_mod, "async_playwright",
                        lambda: (_ for _ in ()).throw(AssertionError("browser used")))

    result = await WebScraper().scrape("https://example.test", engine=engine)

    assert result["success"] is False
    assert result["metadata"]["reason"] == "transport_error"
    assert result["metadata"]["status_code"] is None


@pytest.mark.parametrize("engine", ["auto", "http"])
async def test_non_2xx_http_response_is_not_escalated_or_success(monkeypatch, engine):
    async def failed_response(url):
        return _http_response(503)

    monkeypatch.setattr(fetch, "fetch_http", failed_response)
    monkeypatch.setattr(scraper_mod, "async_playwright",
                        lambda: (_ for _ in ()).throw(AssertionError("browser used")))

    result = await WebScraper().scrape("https://example.test", engine=engine)

    assert result["success"] is False
    assert result["metadata"]["reason"] == "http_status_error"
    assert result["metadata"]["status_code"] == 503


async def test_auto_keeps_successful_non_html_response_off_browser(monkeypatch):
    response = _http_response()
    response["content_type"] = "application/json"

    async def json_response(url):
        return response

    monkeypatch.setattr(fetch, "fetch_http", json_response)
    monkeypatch.setattr(scraper_mod, "async_playwright",
                        lambda: (_ for _ in ()).throw(AssertionError("browser used")))
    scraper = WebScraper()
    monkeypatch.setattr(scraper, "_build_result", lambda *args, **kwargs: {"success": True})

    assert await scraper.scrape("https://example.test", engine="auto") == {"success": True}


class _Browser:
    async def new_context(self, **kwargs):
        return _Context()

    async def close(self):
        pass


class _Context:
    async def route(self, *args):
        pass

    async def route_web_socket(self, *args):
        pass

    async def new_page(self):
        return self.page


class _Playwright:
    def __init__(self, page):
        self.chromium = SimpleNamespace(launch=self._launch)
        self._page = page

    async def _launch(self, **kwargs):
        browser = _Browser()
        context = _Context()
        context.page = self._page
        browser.new_context = lambda **kwargs: _async_value(context)
        return browser


async def _async_value(value):
    return value


class _PlaywrightManager:
    def __init__(self, page):
        self.playwright = _Playwright(page)

    async def __aenter__(self):
        return self.playwright

    async def __aexit__(self, *args):
        pass


class _ChallengePage:
    async def goto(self, *args, **kwargs):
        return SimpleNamespace(status=200)

    async def content(self):
        return "<html>challenge</html>"

    async def title(self):
        raise AssertionError("challenge content must return before title extraction")


class _MetaPage:
    async def goto(self, *args, **kwargs):
        return SimpleNamespace(status=200)

    async def content(self):
        return "<html><body>rendered</body></html>"

    async def title(self):
        return "Rendered title"

    async def screenshot(self, **kwargs):
        return None

    async def evaluate(self, script):
        assert "querySelector" in script
        assert "og:description" in script
        return "immediate description"


class _FailedNavigationPage:
    async def goto(self, *args, **kwargs):
        return SimpleNamespace(status=404)

    async def content(self):
        return "<html><body>Not found</body></html>"

    async def title(self):
        raise AssertionError("failed navigation must return before extraction")


async def _public_url(url):
    return []


async def _no_stealth(page):
    pass


async def test_browser_challenge_returns_blocked_outcome_before_cleaning(monkeypatch):
    monkeypatch.setattr(scraper_mod, "async_playwright",
                        lambda: _PlaywrightManager(_ChallengePage()))
    monkeypatch.setattr(scraper_mod, "ensure_public_url", _public_url)
    monkeypatch.setattr(scraper_mod, "stealth_async", _no_stealth)
    monkeypatch.setattr(fetch, "is_challenge_html", lambda html: True, raising=False)
    scraper = WebScraper()
    monkeypatch.setattr(scraper, "_build_result",
                        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("cleaned challenge")))

    result = await scraper.scrape("https://example.test", engine="browser")

    assert result["success"] is False
    assert result["metadata"]["reason"] == "blocked_challenge"
    assert result["metadata"]["status_code"] == 200


async def test_browser_reads_description_with_immediate_dom_evaluation(monkeypatch):
    monkeypatch.setattr(scraper_mod, "async_playwright",
                        lambda: _PlaywrightManager(_MetaPage()))
    monkeypatch.setattr(scraper_mod, "ensure_public_url", _public_url)
    monkeypatch.setattr(scraper_mod, "stealth_async", _no_stealth)
    monkeypatch.setattr(fetch, "is_challenge_html", lambda html: False, raising=False)
    scraper = WebScraper()
    captured = {}

    def build_result(*args, **kwargs):
        captured.update(kwargs)
        return {"success": True, "metadata": {}, "_raw": {}}

    monkeypatch.setattr(scraper, "_build_result", build_result)

    result = await scraper.scrape("https://example.test", engine="browser")

    assert result["success"] is True
    assert captured["description"] == "immediate description"


async def test_browser_non_2xx_navigation_is_not_success(monkeypatch):
    monkeypatch.setattr(scraper_mod, "async_playwright",
                        lambda: _PlaywrightManager(_FailedNavigationPage()))
    monkeypatch.setattr(scraper_mod, "ensure_public_url", _public_url)
    monkeypatch.setattr(scraper_mod, "stealth_async", _no_stealth)

    result = await WebScraper().scrape("https://example.test", engine="browser")

    assert result["success"] is False
    assert result["metadata"]["reason"] == "http_status_error"
    assert result["metadata"]["status_code"] == 404


async def test_browser_budget_is_reserved_before_launch(monkeypatch):
    monkeypatch.setattr(scraper_mod, "ensure_public_url", _public_url)
    monkeypatch.setattr(
        scraper_mod,
        "async_playwright",
        lambda: (_ for _ in ()).throw(AssertionError("browser launched")),
    )

    async def denied():
        return False

    result = await WebScraper().scrape(
        "https://example.test", engine="browser", before_browser=denied,
    )

    assert result["success"] is False
    assert result["metadata"]["reason"] == "browser_budget_exhausted"
