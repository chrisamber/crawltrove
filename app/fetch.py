"""Tiered fetching with a cheap HTTP tier before browser rendering.

Tier 1 is a plain HTTP GET through curl_cffi with Chrome TLS impersonation —
no browser, ~10x cheaper than a Playwright render. Tier 2 (Playwright, in
scraper.py) is only used when a successful HTML response looks like a JS shell
or bot challenge.
"""
import asyncio
import inspect
import ipaddress
from typing import Any, Dict, Optional
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from curl_cffi import CurlOpt
from curl_cffi.requests import AsyncSession

from app.url_safety import UnsafeUrlError, ensure_public_url

# Lowercase substrings that mark bot-challenge interstitials
CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser",
    "verify you are human",
    "enable javascript and cookies",
    "cf-browser-verification",
    "attention required",
    "captcha",
)
STRONG_CHALLENGE_MARKERS = (
    "checking your browser",
    "verify you are human",
    "enable javascript and cookies",
    "cf-browser-verification",
)
DOM_RENDER_MARKERS = (
    ".append(",
    ".appendchild(",
    ".innerhtml",
    ".insertadjacenthtml(",
    "document.createelement(",
    "document.write(",
)

# A short page needs explicit application structure before it is an SPA shell.
MIN_VISIBLE_TEXT = 400
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
MAX_REDIRECTS = 10
DEFAULT_MAX_DECODED_BYTES = 10 * 1024 * 1024


class HttpFetcher:
    """A reusable HTTP session with SSRF validation and bounded response bodies."""

    def __init__(self, session_factory=None, *, proxy: str | None = None,
                 proxy_auth: tuple[str, str] | None = None):
        self._session_factory = session_factory or AsyncSession
        self._proxy = proxy
        self._proxy_auth = proxy_auth
        self._session = None
        # DNS pin state is session-wide, so serialize requests that update it.
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lock:
            self._start_unlocked()

    def _start_unlocked(self) -> None:
        if self._session is None:
            kwargs = {"impersonate": "chrome", "trust_env": False}
            if self._proxy is not None:
                kwargs["proxy"] = self._proxy
            if self._proxy_auth is not None:
                kwargs["proxy_auth"] = self._proxy_auth
            self._session = self._session_factory(**kwargs)

    async def close(self) -> None:
        async with self._lock:
            session, self._session = self._session, None
            if session is not None and hasattr(session, "close"):
                result = session.close()
                if inspect.isawaitable(result):
                    await result

    async def fetch(self, url: str, timeout_s: int = 60,
                    max_decoded_bytes: int = DEFAULT_MAX_DECODED_BYTES) -> Optional[Dict[str, Any]]:
        """Fetch one public URL without exceeding its decoded body allowance."""
        async with self._lock:
            self._start_unlocked()
            return await self._fetch(url, timeout_s, max_decoded_bytes)

    async def fetch_request(self, url: str, method: str = "GET", *,
                            headers: Optional[Dict[str, str]] = None,
                            data: Optional[bytes] = None,
                            max_decoded_bytes: int = DEFAULT_MAX_DECODED_BYTES
                            ) -> Optional[Dict[str, Any]]:
        """Fetch exactly one validated, DNS-pinned HTTP hop for a browser route."""
        async with self._lock:
            self._start_unlocked()
            try:
                addresses = await ensure_public_url(url)
                self._pin(url, addresses)
                response = await self._request(
                    url, 60, method=method, headers=headers, data=data,
                )
                try:
                    self._verify_connected(response, addresses)
                    return {
                        "status": response.status_code,
                        "headers": dict(response.headers),
                        "content": await self._body(response, max_decoded_bytes),
                        "final_url": str(response.url),
                    }
                finally:
                    await self._close_stream(response)
            except UnsafeUrlError:
                raise
            except Exception:
                return None

    async def _fetch(self, url: str, timeout_s: int,
                     max_decoded_bytes: int) -> Optional[Dict[str, Any]]:
        try:
            async with asyncio.timeout(timeout_s):
                current_url = url
                for redirects in range(MAX_REDIRECTS + 1):
                    addresses = await ensure_public_url(current_url)
                    self._pin(current_url, addresses)

                    response = await self._request(current_url, timeout_s)
                    try:
                        self._verify_connected(response, addresses)
                        location = response.headers.get("location")
                        if response.status_code in REDIRECT_STATUSES and location:
                            if redirects == MAX_REDIRECTS:
                                return None
                            current_url = urljoin(current_url, location)
                            continue
                        content = await self._body(response, max_decoded_bytes)
                        content_type = response.headers.get("content-type", "")
                        encoding = getattr(response, "encoding", None) or "utf-8"
                        return {
                            "status": response.status_code,
                            "html": content.decode(encoding, "replace"),
                            "content": content,
                            "headers": dict(response.headers),
                            "final_url": str(response.url),
                            "content_type": content_type,
                        }
                    finally:
                        await self._close_stream(response)
        except UnsafeUrlError:
            raise
        except Exception:
            return None

    def _pin(self, url: str, addresses) -> None:
        if addresses:
            parsed = urlsplit(url)
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            host = parsed.hostname.encode("idna").decode("ascii")
            try:
                ipaddress.ip_address(host)
            except ValueError:
                pinned = ",".join(
                    f"[{address}]" if ":" in address else address
                    for address in addresses
                )
                self._session.curl_options = {CurlOpt.RESOLVE: [f"{host}:{port}:{pinned}"]}
            else:
                self._session.curl_options = {}

    def _verify_connected(self, response, addresses) -> None:
        if addresses:
            try:
                connected_ip = str(ipaddress.ip_address(response.primary_ip))
            except (TypeError, ValueError) as exc:
                raise UnsafeUrlError("Could not verify the connected server address") from exc
            if connected_ip not in addresses:
                raise UnsafeUrlError("Connected server address changed after validation")

    async def _request(self, url: str, timeout_s: int, *, method: str = "GET",
                       headers: Optional[Dict[str, str]] = None,
                       data: Optional[bytes] = None):
        if method == "GET" and hasattr(self._session, "stream"):
            stream = self._session.stream(
                method, url, timeout=(10, timeout_s), allow_redirects=False,
                headers=headers,
            )
            async with asyncio.timeout(20):
                response = await stream.__aenter__()
            response._crawltrove_stream = stream
            return response
        if method == "GET" and not headers and data is None:
            return await self._session.get(url, timeout=timeout_s, allow_redirects=False)
        return await self._session.request(
            method, url, timeout=timeout_s, allow_redirects=False,
            headers=headers, data=data,
        )

    async def _body(self, response, max_decoded_bytes: int) -> bytes:
        try:
            if hasattr(response, "aiter_content"):
                chunks = []
                size = 0
                async for chunk in response.aiter_content():
                    size += len(chunk)
                    if size > max_decoded_bytes:
                        raise ValueError("HTTP response exceeds decoded byte limit")
                    chunks.append(chunk)
                return b"".join(chunks)
            content = response.content
            if len(content) > max_decoded_bytes:
                raise ValueError("HTTP response exceeds decoded byte limit")
            return content
        finally:
            await self._close_stream(response)

    async def _close_stream(self, response) -> None:
        stream = getattr(response, "_crawltrove_stream", None)
        if stream is not None:
            response._crawltrove_stream = None
            await stream.__aexit__(None, None, None)


_shared_fetcher = HttpFetcher()


async def fetch_http(
    url: str,
    timeout_s: int = 20,
    max_decoded_bytes: int = DEFAULT_MAX_DECODED_BYTES,
    *,
    proxy: str | None = None,
    proxy_auth: tuple[str, str] | None = None,
) -> Optional[Dict[str, Any]]:
    """Fetch through the process-local reusable session."""
    if proxy is not None:
        # Proxy DNS pinning and curl options are request-specific.  Do not let a
        # leased worker mutate the shared direct-fetch session.
        fetcher = HttpFetcher(proxy=proxy, proxy_auth=proxy_auth)
        try:
            return await fetcher.fetch(
                url, timeout_s=timeout_s, max_decoded_bytes=max_decoded_bytes,
            )
        finally:
            await fetcher.close()

    global _shared_fetcher
    if _shared_fetcher._session_factory is not AsyncSession:
        await _shared_fetcher.close()
        _shared_fetcher = HttpFetcher()
    return await _shared_fetcher.fetch(
        url, timeout_s=timeout_s, max_decoded_bytes=max_decoded_bytes,
    )


async def close_http_fetcher() -> None:
    """Release the shared HTTP session during application shutdown."""
    await _shared_fetcher.close()


def needs_browser(response: Optional[Dict[str, Any]]) -> bool:
    """Return whether a successful HTML response needs browser rendering."""
    status = response.get("status") if response else None
    if not isinstance(status, int) or not 200 <= status < 300:
        return False

    content_type = response.get("content_type", "").split(";", 1)[0].strip().lower()
    if content_type not in {"text/html", "application/xhtml+xml"}:
        return False

    html = response.get("html") or ""
    if is_challenge_html(html):
        return True

    # A short complete page is valid. Escalate only when its structure also
    # identifies it as an application shell that requires JavaScript.
    soup = BeautifulSoup(html, "html.parser")
    roots = soup.select("#__next, #__nuxt, #root, #app, [data-reactroot]")
    has_hydration = bool(soup.select("[data-reactroot], [data-reactid]"))
    has_script_bundle = bool(soup.select("script[src]"))
    inline_scripts = " ".join(
        script.get_text(" ", strip=True).lower()
        for script in soup.select("script:not([src])")
    )
    has_inline_dom_render = any(
        marker in inline_scripts for marker in DOM_RENDER_MARKERS
    )
    for el in soup(["script", "style", "noscript", "template"]):
        el.decompose()
    text = soup.get_text(" ", strip=True)
    if len(text) >= MIN_VISIBLE_TEXT:
        return False

    return (
        bool(roots) and (has_hydration or has_script_bundle)
    ) or has_inline_dom_render


def is_challenge_html(html: str) -> bool:
    """Return whether HTML looks like a bot-challenge interstitial."""
    lowered = html[:20000].lower()
    if any(marker in lowered for marker in STRONG_CHALLENGE_MARKERS):
        return True
    if not any(marker in lowered for marker in CHALLENGE_MARKERS):
        return False
    soup = BeautifulSoup(lowered, "html.parser")
    return bool(soup.select(
        "[id*='challenge'], [class*='challenge'], [id*='captcha'], "
        "[class*='captcha'], [class*='turnstile'], iframe[src*='challenge'], "
        "iframe[src*='captcha']"
    ))
