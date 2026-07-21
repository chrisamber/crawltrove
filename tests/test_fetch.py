import pytest

from app.fetch import is_challenge_html, needs_browser


def _response(html="<p>OK</p>", status=200, content_type="text/html"):
    return {"status": status, "html": html, "content_type": content_type}


def test_is_challenge_html_uses_known_markers():
    assert is_challenge_html("<title>Checking your browser</title>")
    assert is_challenge_html('<div class="cf-turnstile">Just a moment</div>')
    assert not is_challenge_html("<article>How CAPTCHA systems work</article>")
    assert not is_challenge_html("<p>A normal page</p>")


def test_needs_browser_escalates_successful_html_challenge():
    assert needs_browser(_response("<p>Verify you are human</p>"))


def test_needs_browser_escalates_short_hydrated_application_shell():
    assert needs_browser(_response('<div id="root"></div><script src="/app.js"></script>'))


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
