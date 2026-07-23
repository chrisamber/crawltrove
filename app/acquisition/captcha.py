"""Fail-closed handling for operator-authorized image/text CAPTCHAs."""
from __future__ import annotations

import asyncio
import base64
import inspect
import os
import re
import stat
import warnings
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from PIL import Image, ImageEnhance, ImageOps, UnidentifiedImageError

from app.acquisition.profiles import host_allowed


MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
SOLVER_TIMEOUT_SECONDS = 10
_ANSWER = re.compile(r"[A-Za-z0-9]{3,12}\Z")


@dataclass(frozen=True)
class CaptchaPolicy:
    """Operator-provided host allowlist; jobs never choose solver targets."""

    domains: tuple[str, ...]

    @classmethod
    def parse(cls, value: str | None) -> "CaptchaPolicy":
        domains = tuple(part.strip() for part in (value or "").split(",") if part.strip())
        for domain in domains:
            # host_allowed validates patterns with the shared public-suffix rules.
            host_allowed("validation.invalid", [domain])
        return cls(domains)

    @classmethod
    def from_environment(cls) -> "CaptchaPolicy":
        return cls.parse(os.environ.get("CAPTCHA_AUTHORIZED_DOMAINS"))

    def allows(self, host: str) -> bool:
        if not self.domains:
            return False
        try:
            return host_allowed(host, self.domains)
        except ValueError:
            return False

    def allows_url(self, value: str) -> bool:
        try:
            parsed = urlsplit(value)
        except ValueError:
            return False
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        if parsed.username or parsed.password:
            return False
        return self.allows(parsed.hostname)


@dataclass(frozen=True)
class CaptchaChallenge:
    kind: str
    image: Any | None = None
    text_input: Any | None = None
    submit: Any | None = None


@dataclass(frozen=True)
class CaptchaResult:
    state: str
    kind: str | None = None


class ImageTextSolver(Protocol):
    async def solve(self, image: bytes, *, host: str) -> str | None:
        """Return a validated answer, or ``None`` when no safe answer exists."""


def _confidence_threshold(value: str | None = None) -> float:
    try:
        threshold = float(value if value is not None else os.environ.get(
            "CAPTCHA_SOLVER_MIN_CONFIDENCE", "0.80"
        ))
    except ValueError as exc:
        raise ValueError("CAPTCHA_SOLVER_MIN_CONFIDENCE must be a number") from exc
    if not 0 <= threshold <= 1:
        raise ValueError("CAPTCHA_SOLVER_MIN_CONFIDENCE must be between 0 and 1")
    return threshold


def _answer(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    answer = value.strip()
    return answer if _ANSWER.fullmatch(answer) else None


def load_solver_token(path: str | os.PathLike[str]) -> str:
    """Read the configured solver secret only from an exact mode-0600 file."""
    token_path = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(token_path, flags)
    except OSError as exc:
        raise PermissionError(
            "CAPTCHA_SOLVER_TOKEN_FILE must be a regular non-symlink file"
        ) from exc
    with os.fdopen(descriptor, encoding="utf-8") as token_stream:
        metadata = os.fstat(token_stream.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise PermissionError("CAPTCHA_SOLVER_TOKEN_FILE must be a regular file")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise PermissionError("CAPTCHA_SOLVER_TOKEN_FILE must have mode 0600")
        token = token_stream.read().strip()
    if not token:
        raise ValueError("CAPTCHA_SOLVER_TOKEN_FILE is empty")
    return token


def _decode_image(image: bytes) -> Image.Image:
    if not isinstance(image, bytes) or not image or len(image) > MAX_IMAGE_BYTES:
        raise ValueError("CAPTCHA image exceeds the 5 MiB limit")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(image)) as opened:
                if (opened.width <= 0 or opened.height <= 0
                        or opened.width * opened.height > MAX_IMAGE_PIXELS):
                    raise ValueError("CAPTCHA image has invalid dimensions")
                opened.verify()
            with Image.open(BytesIO(image)) as opened:
                opened.load()
                return ImageOps.grayscale(opened.copy())
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as exc:
        raise ValueError("CAPTCHA image is invalid") from exc


def _ocr_image(image: bytes) -> tuple[str | None, float]:
    """Run local OCR only on decoded image bytes, never on page content."""
    import pytesseract

    normalized = ImageEnhance.Contrast(_decode_image(image)).enhance(2)
    data = pytesseract.image_to_data(
        normalized, config="--psm 8", output_type=pytesseract.Output.DICT
    )
    words = []
    scores = []
    for text, confidence in zip(data.get("text", ()), data.get("conf", ())):
        candidate = _answer(text)
        try:
            score = float(confidence) / 100
        except (TypeError, ValueError):
            continue
        if candidate and score >= 0:
            words.append(candidate)
            scores.append(score)
    if len(words) != 1 or len(scores) != 1:
        return None, 0.0
    return words[0], scores[0]


class LocalImageTextSolver:
    """Tesseract implementation used only for the current challenge image."""

    def __init__(self, *, min_confidence: float | None = None) -> None:
        self._min_confidence = _confidence_threshold(
            None if min_confidence is None else str(min_confidence)
        )

    async def solve(self, image: bytes, *, host: str) -> str | None:
        del host
        answer, confidence = await asyncio.to_thread(_ocr_image, image)
        return answer if confidence >= self._min_confidence else None


class HttpImageTextSolver:
    """Narrow client for a trusted operator-configured OCR service."""

    def __init__(
        self,
        url: str,
        *,
        token: str,
        transport: httpx.AsyncBaseTransport | None = None,
        min_confidence: float | None = None,
    ) -> None:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("CAPTCHA_SOLVER_URL must be an HTTPS URL without credentials")
        if not token:
            raise ValueError("CAPTCHA solver token is required")
        self._url = url
        self._token = token
        self._transport = transport
        self._min_confidence = _confidence_threshold(
            None if min_confidence is None else str(min_confidence)
        )

    @classmethod
    def from_environment(cls) -> "HttpImageTextSolver | None":
        url = os.environ.get("CAPTCHA_SOLVER_URL", "").strip()
        if not url:
            return None
        token_file = os.environ.get("CAPTCHA_SOLVER_TOKEN_FILE", "").strip()
        if not token_file:
            raise ValueError("CAPTCHA_SOLVER_TOKEN_FILE is required with CAPTCHA_SOLVER_URL")
        return cls(url, token=load_solver_token(token_file))

    async def solve(self, image: bytes, *, host: str) -> str | None:
        _decode_image(image)
        payload = {
            "kind": "image_text",
            "host": host,
            "imageBase64": base64.b64encode(image).decode("ascii"),
        }
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=SOLVER_TIMEOUT_SECONDS,
                follow_redirects=False, trust_env=False,
            ) as client:
                response = await client.post(
                    self._url,
                    json=payload,
                    headers={"authorization": f"Bearer {self._token}"},
                )
            if response.status_code != 200:
                return None
            result = response.json()
            confidence = float(result.get("confidence", -1))
        except (httpx.HTTPError, ValueError, TypeError):
            return None
        answer = _answer(result.get("answer"))
        return answer if answer and confidence >= self._min_confidence else None


async def _visible(elements: list[Any]) -> list[Any]:
    found = []
    for element in elements:
        try:
            if await element.is_visible():
                found.append(element)
        except Exception:
            continue
    return found


async def classify_challenge(page: Any) -> CaptchaChallenge | None:
    """Classify token challenges before considering image capture or OCR."""
    try:
        markup = (await page.content())[:200_000].lower()
    except Exception:
        return None
    if "recaptcha" in markup or "google.com/recaptcha" in markup:
        return CaptchaChallenge("recaptcha")
    if "hcaptcha" in markup or "h-captcha" in markup:
        return CaptchaChallenge("hcaptcha")
    # Match a host-like Cloudflare Turnstile origin rather than a bare substring.
    if "turnstile" in markup or re.search(
        r"(?:https?:)?//challenges\.cloudflare\.com(?:/|[\"'\s>]|$)",
        markup,
    ):
        return CaptchaChallenge("turnstile")
    if "data-sitekey" in markup:
        return CaptchaChallenge("managed")
    try:
        forms = await page.query_selector_all("form")
    except Exception:
        return None
    for form in forms:
        try:
            images = await _visible(await form.query_selector_all("img"))
            inputs = await _visible(await form.query_selector_all(
                "input[type='text'], input[type='search'], input[type='tel'], input:not([type])"
            ))
            submits = await _visible(
                await form.query_selector_all(
                    "button[type='submit'], button:not([type]), input[type='submit']"
                )
            )
        except Exception:
            continue
        if len(images) == len(inputs) == len(submits) == 1:
            return CaptchaChallenge("image_text", images[0], inputs[0], submits[0])
    if "captcha" in markup or "challenge" in markup:
        return CaptchaChallenge("ambiguous")
    return None


async def _wait_once(page: Any) -> None:
    waiter = getattr(page, "wait_for_load_state", None)
    if waiter is None:
        return
    try:
        result = waiter("domcontentloaded", timeout=SOLVER_TIMEOUT_SECONDS * 1000)
        if inspect.isawaitable(result):
            await asyncio.wait_for(result, timeout=SOLVER_TIMEOUT_SECONDS)
    except Exception:
        return


async def solve_if_authorized(
    page: Any,
    policy: CaptchaPolicy,
    *,
    solver: ImageTextSolver | None = None,
) -> CaptchaResult:
    """Perform at most one authorized image/text solve and one form submission."""
    initial_url = getattr(page, "url", "")
    if not policy.allows_url(initial_url):
        return CaptchaResult("not_authorized")
    challenge = await classify_challenge(page)
    if challenge is None:
        return CaptchaResult("not_image_text_challenge")
    return await solve_image_text(page, challenge, policy, solver=solver)


async def solve_image_text(
    page: Any,
    challenge: CaptchaChallenge,
    policy: CaptchaPolicy,
    *,
    solver: ImageTextSolver | None = None,
) -> CaptchaResult:
    """Solve one already-classified challenge through the same guarded path."""
    initial_url = getattr(page, "url", "")
    if not policy.allows_url(initial_url):
        return CaptchaResult("not_authorized")
    if challenge.kind != "image_text":
        return CaptchaResult("requires_human_or_provider", challenge.kind)
    try:
        image = await challenge.image.screenshot(type="png")
        _decode_image(image)
    except Exception:
        return CaptchaResult("requires_human_or_provider", challenge.kind)
    solver = solver or HttpImageTextSolver.from_environment() or LocalImageTextSolver()
    try:
        answer = await asyncio.wait_for(
            solver.solve(image, host=urlsplit(initial_url).hostname or ""),
            timeout=SOLVER_TIMEOUT_SECONDS,
        )
    except Exception:
        return CaptchaResult("requires_human_or_provider", challenge.kind)
    if not _answer(answer):
        return CaptchaResult("requires_human_or_provider", challenge.kind)
    try:
        await challenge.text_input.fill(answer)
        await challenge.submit.click()
        await _wait_once(page)
    except Exception:
        return CaptchaResult("requires_human_or_provider", challenge.kind)
    if not policy.allows_url(getattr(page, "url", "")):
        return CaptchaResult("final_host_not_authorized", challenge.kind)
    return CaptchaResult("submitted", challenge.kind)
