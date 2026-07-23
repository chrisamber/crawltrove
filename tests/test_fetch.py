import pytest

from app.fetch import HttpFetcher, is_challenge_html, needs_browser


def _response(html="<p>OK</p>", status=200, content_type="text/html"):
    return {"status": status, "html": html, "content_type": content_type}


def test_is_challenge_html_uses_known_markers():
    assert is_challenge_html("<title>Checking your browser</title>")
    assert is_challenge_html('<div class="cf-turnstile">Just a moment</div>')
    assert not is_challenge_html("<article>How CAPTCHA systems work</article>")
    assert not is_challenge_html("<p>A normal page</p>")


def test_needs_browser_escalates_successful_html_challenge():
    assert needs_browser(_response("<p>Verify you are human</p>"))


async def test_http_fetcher_reuses_one_session(monkeypatch):
    class Response:
        status_code = 200
        headers = {"content-type": "text/html"}
        content = b"ok"
        url = "https://example.com"
        primary_ip = "93.184.216.34"

    class Session:
        def __init__(self, **kwargs):
            self.curl_options = {}

        async def get(self, url, **kwargs):
            return Response()

        async def close(self):
            pass

    calls = []

    def factory(**kwargs):
        calls.append(kwargs)
        return Session(**kwargs)

    async def public_url(url):
        return ()

    monkeypatch.setattr("app.fetch.ensure_public_url", public_url)
    fetcher = HttpFetcher(session_factory=factory)
    await fetcher.start()
    await fetcher.fetch("https://example.com/a", max_decoded_bytes=1024)
    await fetcher.fetch("https://example.com/b", max_decoded_bytes=1024)
    await fetcher.close()

    assert len(calls) == 1


async def test_http_fetcher_rejects_body_over_decoded_limit(monkeypatch):
    class Response:
        status_code = 200
        headers = {"content-type": "text/html"}
        content = b"too large"
        url = "https://example.com"
        primary_ip = "93.184.216.34"

    class Session:
        def __init__(self, **kwargs):
            self.curl_options = {}

        async def get(self, url, **kwargs):
            return Response()

        async def close(self):
            pass

    async def public_url(url):
        return ()

    monkeypatch.setattr("app.fetch.ensure_public_url", public_url)
    fetcher = HttpFetcher(session_factory=Session)
    assert await fetcher.fetch(
        "https://example.com", max_decoded_bytes=3,
    ) is None
    await fetcher.close()


async def test_pinned_fetch_validates_each_redirect_destination(monkeypatch):
    from app import fetch
    from curl_cffi import CurlOpt

    class Response:
        def __init__(self, status, url, primary_ip, headers):
            self.status_code = status
            self.url = url
            self.primary_ip = primary_ip
            self.headers = headers
            self.content = b"ok"

    class Session:
        def __init__(self, **kwargs):
            self.curl_options = {}
            self.requests = []

        async def get(self, url, **kwargs):
            self.requests.append((url, dict(self.curl_options)))
            if url.endswith("/start"):
                return Response(302, url, "93.184.216.34", {"location": "https://next.example/final"})
            return Response(200, url, "93.184.216.35", {"content-type": "text/html"})

    checked = []

    async def validate(url):
        checked.append(url)
        return ("93.184.216.34",) if "public" in url else ("93.184.216.35",)

    monkeypatch.setattr(fetch, "ensure_public_url", validate)
    session = Session()
    fetcher = fetch.HttpFetcher(session_factory=lambda **kwargs: session)
    result = await fetcher.fetch("https://public.example/start")
    await fetcher.close()

    assert result["final_url"] == "https://next.example/final"
    assert checked == ["https://public.example/start", "https://next.example/final"]
    assert session.requests[0][1][CurlOpt.RESOLVE] == ["public.example:443:93.184.216.34"]
    assert session.requests[1][1][CurlOpt.RESOLVE] == ["next.example:443:93.184.216.35"]


def test_needs_browser_escalates_short_hydrated_application_shell():
    assert needs_browser(_response('<div id="root"></div><script src="/app.js"></script>'))


def test_needs_browser_escalates_short_shell_with_inline_dom_render():
    html = (
        '<main class="quotes"></main>'
        '<script src="/jquery.js"></script>'
        '<script>$(".quotes").append("<article>Loaded by JavaScript</article>")</script>'
    )

    assert needs_browser(_response(html))


def test_needs_browser_keeps_complete_short_html_on_http():
    assert not needs_browser(_response("<main><h1>Thanks</h1><p>We received it.</p></main>"))


def test_needs_browser_requires_hydration_evidence_beside_an_app_root():
    assert not needs_browser(_response('<div id="root">Thanks</div>'))


@pytest.mark.parametrize("response", [
    None,
    _response(status=199),
    _response(status=300),
    _response(status=429),
    _response(status=503),
])
def test_needs_browser_does_not_escalate_transport_or_non_success(response):
    assert not needs_browser(response)


@pytest.mark.parametrize("content_type", [
    "",
    "text/plain",
    "text/markdown",
    "application/json",
    "application/xml",
    "application/rss+xml",
    "application/pdf",
    "image/png",
])
def test_needs_browser_does_not_escalate_non_html_content(content_type):
    assert not needs_browser(_response('<div id="root"></div><script src="/app.js"></script>',
                                       content_type=content_type))
