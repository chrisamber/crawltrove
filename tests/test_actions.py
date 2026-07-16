"""Pre-scrape actions: dispatch, resilience, engine forcing, API validation.

run_actions is driven with a fake page object (the Playwright tier is never
launched in tests); the API layer is exercised over the ASGI transport.
"""
import httpx
import pytest

from app import actions


class FakeKeyboard:
    def __init__(self, calls):
        self.calls = calls

    async def press(self, key):
        self.calls.append(("press", key))


class FakePage:
    def __init__(self, fail_selectors=()):
        self.calls = []
        self.fail_selectors = set(fail_selectors)
        self.keyboard = FakeKeyboard(self.calls)

    async def wait_for_timeout(self, ms):
        self.calls.append(("wait_for_timeout", ms))

    async def wait_for_selector(self, selector, timeout=None):
        self.calls.append(("wait_for_selector", selector))

    async def click(self, selector, timeout=None):
        if selector in self.fail_selectors:
            raise RuntimeError(f"no element matches {selector}")
        self.calls.append(("click", selector))

    async def fill(self, selector, text, timeout=None):
        self.calls.append(("fill", selector, text))

    async def evaluate(self, script):
        self.calls.append(("evaluate", script))


async def test_each_action_type_dispatches():
    page = FakePage()
    outcomes = await actions.run_actions(page, [
        {"type": "wait", "milliseconds": 250},
        {"type": "wait", "selector": "#ready"},
        {"type": "click", "selector": "#btn"},
        {"type": "scroll"},
        {"type": "scroll", "direction": "up"},
        {"type": "fill", "selector": "#q", "text": "hello"},
        {"type": "press", "key": "Enter"},
    ])
    assert all(o["ok"] for o in outcomes)
    assert ("wait_for_timeout", 250) in page.calls
    assert ("wait_for_selector", "#ready") in page.calls
    assert ("click", "#btn") in page.calls
    assert ("fill", "#q", "hello") in page.calls
    assert ("press", "Enter") in page.calls
    scrolls = [c for c in page.calls if c[0] == "evaluate"]
    assert "window.scrollBy(0, 0.9" in scrolls[0][1]
    assert "window.scrollBy(0, -0.9" in scrolls[1][1]


async def test_wait_ms_is_clamped():
    page = FakePage()
    await actions.run_actions(page, [{"type": "wait", "milliseconds": 999999}])
    assert page.calls == [("wait_for_timeout", actions.MAX_WAIT_MS)]


async def test_failure_is_captured_and_rest_still_run():
    page = FakePage(fail_selectors={"#missing"})
    outcomes = await actions.run_actions(page, [
        {"type": "click", "selector": "#missing"},
        {"type": "press", "key": "Tab"},
    ])
    assert outcomes[0]["ok"] is False
    assert "#missing" in outcomes[0]["error"]
    assert outcomes[1]["ok"] is True
    assert ("press", "Tab") in page.calls


async def test_action_list_is_capped():
    page = FakePage()
    outcomes = await actions.run_actions(
        page, [{"type": "scroll"}] * (actions.MAX_ACTIONS + 5))
    assert len(outcomes) == actions.MAX_ACTIONS


def test_effective_engine_forces_browser():
    assert actions.effective_engine("auto", [{"type": "scroll"}]) == "browser"
    assert actions.effective_engine("http", [{"type": "scroll"}]) == "browser"
    assert actions.effective_engine("auto", None) == "auto"
    assert actions.effective_engine("http", []) == "http"


def _client():
    from app.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")


async def test_api_rejects_bad_actions():
    async with _client() as c:
        bad_type = await c.post("/api/scrape", json={
            "url": "https://e.test", "actions": [{"type": "dance"}]})
        assert bad_type.status_code == 422
        too_many = await c.post("/api/scrape", json={
            "url": "https://e.test",
            "actions": [{"type": "scroll"}] * 21})
        assert too_many.status_code == 422


async def test_api_threads_actions_to_scraper(monkeypatch):
    import app.services as services
    seen = {}

    async def fake_scrape(url, **kw):
        seen.update(kw)
        return {"success": True, "url": url, "title": "t", "description": "",
                "markdown": "# m", "html": "<html/>",
                "metadata": {"url": url, "engine": "browser",
                             "actions": [{"type": "click", "ok": True, "error": None}]}}

    monkeypatch.setattr(services.scraper, "scrape", fake_scrape)
    from app import dedup, storage
    monkeypatch.setattr(dedup, "check_and_register",
                        lambda text, key: {"content_hash": "h",
                                           "exact_duplicate_of": None,
                                           "near_duplicate_of": None})
    monkeypatch.setattr(storage, "save_scrape", lambda r: "stem")
    monkeypatch.setattr(storage, "save_run_raw", lambda *a, **kw: {})

    async with _client() as c:
        resp = await c.post("/api/scrape", json={
            "url": "https://e.test",
            "actions": [{"type": "click", "selector": "#btn"},
                        {"type": "wait", "milliseconds": 100}]})
    assert resp.status_code == 200
    assert seen["actions"] == [{"type": "click", "selector": "#btn"},
                               {"type": "wait", "milliseconds": 100}]
    assert resp.json()["metadata"]["actions"][0]["ok"] is True
