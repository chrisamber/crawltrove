import base64
import json
from io import BytesIO

import httpx
import pytest
from PIL import Image

from app.acquisition.captcha import (
    CaptchaPolicy,
    HttpImageTextSolver,
    classify_challenge,
    load_solver_token,
    solve_image_text,
    solve_if_authorized,
)
from app.captcha_service import create_app


class _Element:
    def __init__(self, *, image: bytes | None = None):
        self.image = image
        self.fills = []
        self.clicks = 0

    async def is_visible(self):
        return True

    async def screenshot(self, **_kwargs):
        return self.image

    async def fill(self, value):
        self.fills.append(value)

    async def click(self):
        self.clicks += 1


class _Form:
    def __init__(self, image):
        self.image = _Element(image=image)
        self.input = _Element()
        self.submit = _Element()

    async def query_selector_all(self, selector):
        if selector == "img":
            return [self.image]
        if selector.startswith("input[type='text']"):
            return [self.input]
        if selector.startswith("button[type='submit']"):
            return [self.submit]
        return []


class _Page:
    def __init__(self, html, *, url="https://example.com/challenge", image=None):
        self.html = html
        self.url = url
        self.form = _Form(image or _png())
        self.fill_calls = self.form.input.fills

    async def content(self):
        return self.html

    async def query_selector_all(self, selector):
        return [self.form] if selector == "form" else []

    async def wait_for_load_state(self, *_args, **_kwargs):
        return None


def _png():
    """A valid small image without storing a CAPTCHA fixture in the repository."""
    output = BytesIO()
    Image.new("L", (1, 1), color=255).save(output, format="PNG")
    return output.getvalue()


def test_captcha_authorization_rechecks_final_host():
    policy = CaptchaPolicy.parse("example.com,*.docs.example.com")
    assert policy.allows("example.com")
    assert policy.allows("v2.docs.example.com")
    assert not policy.allows("example.net")
    assert not policy.allows("www.example.com")
    assert not policy.allows("https://example.com")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["recaptcha", "hcaptcha", "turnstile"])
async def test_token_challenges_never_use_first_party_solver(kind):
    page = _Page(f'<iframe class="{kind}" data-sitekey="site"></iframe>')

    result = await solve_if_authorized(page, CaptchaPolicy.parse("example.com"))

    assert result.state == "requires_human_or_provider"
    assert page.fill_calls == []


@pytest.mark.asyncio
async def test_configured_solver_receives_only_bounded_image():
    captured = []

    async def handler(request):
        captured.append(request)
        return httpx.Response(200, json={"answer": "A7B9", "confidence": 0.95})

    solver = HttpImageTextSolver(
        "https://solver.internal/solve",
        token="secret",
        transport=httpx.MockTransport(handler),
    )

    assert await solver.solve(_png(), host="example.com") == "A7B9"
    payload = json.loads(captured[0].content)
    assert set(payload) == {"kind", "host", "imageBase64"}
    assert "html" not in payload and "cookies" not in payload


def test_configured_solver_token_must_be_mode_0600(tmp_path):
    token_file = tmp_path / "solver-token"
    token_file.write_text("secret\n")
    token_file.chmod(0o644)
    with pytest.raises(PermissionError, match="0600"):
        load_solver_token(token_file)
    token_file.chmod(0o600)
    assert load_solver_token(token_file) == "secret"


def test_configured_solver_token_rejects_symlink(tmp_path):
    token_file = tmp_path / "solver-token"
    token_file.write_text("secret\n")
    token_file.chmod(0o600)
    token_link = tmp_path / "solver-token-link"
    token_link.symlink_to(token_file)

    with pytest.raises(PermissionError, match="non-symlink"):
        load_solver_token(token_link)


@pytest.mark.asyncio
async def test_image_solver_rechecks_final_url_after_one_submit():
    class Solver:
        async def solve(self, image, *, host):
            assert image == _png()
            assert host == "example.com"
            return "A7B9"

    page = _Page('<form><img><input><button></button></form>')

    async def redirect_after_submit(*_args, **_kwargs):
        page.url = "https://example.net/after-submit"

    page.wait_for_load_state = redirect_after_submit
    challenge = await classify_challenge(page)
    result = await solve_image_text(
        page, challenge, CaptchaPolicy.parse("example.com"), solver=Solver()
    )

    assert result.state == "final_host_not_authorized"
    assert page.fill_calls == ["A7B9"]
    assert page.form.submit.clicks == 1


@pytest.mark.asyncio
async def test_captcha_service_accepts_only_small_image_payload():
    app = create_app(
        token="service-secret",
        policy=CaptchaPolicy.parse("example.com"),
        ocr=lambda _image: ("A7B9", 0.95),
    )
    encoded = base64.b64encode(_png()).decode("ascii")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://captcha"
    ) as client:
        response = await client.post(
            "/solve",
            headers={"authorization": "Bearer service-secret"},
            json={"kind": "image_text", "host": "example.com", "imageBase64": encoded},
        )
        invalid = await client.post(
            "/solve",
            headers={"authorization": "Bearer service-secret"},
            json={
                "kind": "image_text", "host": "example.com", "imageBase64": encoded,
                "html": "must not be accepted",
            },
        )

    assert response.status_code == 200
    assert set(response.json()) == {"answer", "confidence"}
    assert invalid.status_code == 422
