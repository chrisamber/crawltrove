"""Chromium launch policy: sandboxed by default with explicit local overrides."""
from app import scraper


def test_launch_kwargs_default(monkeypatch):
    monkeypatch.delenv("CHROMIUM_EXECUTABLE_PATH", raising=False)
    monkeypatch.delenv("CHROMIUM_DISABLE_SANDBOX", raising=False)
    kwargs = scraper._launch_kwargs()
    assert kwargs["headless"] is True
    assert kwargs["chromium_sandbox"] is True
    assert "--disable-dev-shm-usage" in kwargs["args"]
    assert "--no-sandbox" not in kwargs["args"]
    assert "--disable-setuid-sandbox" not in kwargs["args"]
    assert "executable_path" not in kwargs


def test_launch_kwargs_env_override(monkeypatch):
    monkeypatch.setenv("CHROMIUM_EXECUTABLE_PATH", "/opt/browsers/chrome")
    kwargs = scraper._launch_kwargs()
    assert kwargs["executable_path"] == "/opt/browsers/chrome"
    # The rest of the launch config is unchanged by the override.
    assert kwargs["headless"] is True
    assert "--disable-gpu" in kwargs["args"]


def test_launch_kwargs_explicit_unsafe_sandbox_override(monkeypatch):
    monkeypatch.setenv("CHROMIUM_DISABLE_SANDBOX", "true")
    kwargs = scraper._launch_kwargs()
    assert kwargs["chromium_sandbox"] is False
    assert "--no-sandbox" in kwargs["args"]


def test_browser_context_blocks_service_workers():
    kwargs = scraper._context_kwargs()
    assert kwargs["ignore_https_errors"] is False
    assert kwargs["service_workers"] == "block"


async def test_screenshot_disabled_never_calls_page_screenshot():
    class Page:
        screenshot_calls = 0

        async def goto(self, *args, **kwargs):
            return type("Response", (), {"status": 200})()

        async def content(self):
            return "<html><body>ok</body></html>"

        async def title(self):
            return "OK"

        async def evaluate(self, script):
            return ""

        async def screenshot(self, **kwargs):
            self.screenshot_calls += 1
            return b"png"

    class Context:
        async def route(self, *args):
            pass

        async def route_web_socket(self, *args):
            pass

        async def new_page(self):
            return page

        async def close(self):
            pass

    class Browser:
        async def new_context(self, **kwargs):
            return Context()

        async def close(self):
            pass

    class Playwright:
        class chromium:
            @staticmethod
            async def launch(**kwargs):
                return Browser()

    class Manager:
        async def __aenter__(self):
            return Playwright()

        async def __aexit__(self, *args):
            pass

    from app.scraper import BrowserRuntime

    page = Page()
    runtime = BrowserRuntime(lambda: Manager())
    await runtime.render("https://example.com", capture_screenshot=False,
                         max_dom_bytes=1024)
    await runtime.close()
    assert page.screenshot_calls == 0


async def test_browser_route_aborts_when_pinned_byte_allowance_is_exhausted():
    from app.scraper import BrowserRuntime

    class Transport:
        async def fetch_request(self, *args, **kwargs):
            return {"status": 200, "headers": {}, "content": b"12345"}

        async def close(self):
            pass

    class Request:
        url = "https://example.com"
        method = "GET"
        headers = {}
        post_data_buffer = None

        def is_navigation_request(self):
            return True

    class Route:
        request = Request()
        aborted = False

        async def abort(self, reason):
            self.aborted = reason

        async def fulfill(self, **kwargs):
            raise AssertionError("over-budget content must not fulfill")

    route = Route()

    class Page:
        async def goto(self, *args, **kwargs):
            await context.handler(route)
            return type("Response", (), {"status": 200})()

        async def content(self): return "<html/>"
        async def title(self): return ""
        async def evaluate(self, script): return ""

    class Context:
        async def route(self, pattern, handler): self.handler = handler
        async def route_web_socket(self, *args): pass
        async def new_page(self): return Page()
        async def close(self): pass

    context = Context()

    class Browser:
        async def new_context(self, **kwargs): return context
        async def close(self): pass

    class Playwright:
        class chromium:
            @staticmethod
            async def launch(**kwargs): return Browser()

    class Manager:
        async def __aenter__(self): return Playwright()
        async def __aexit__(self, *args): pass

    runtime = BrowserRuntime(lambda: Manager(), transport_factory=Transport)
    await runtime.render("https://example.com", max_decoded_bytes=3)
    await runtime.close()
    assert route.aborted == "blockedbyclient"
