"""Tiered fetching with a cheap HTTP tier before browser rendering.

Tier 1 is a plain HTTP GET through curl_cffi with Chrome TLS impersonation —
no browser, ~10x cheaper than a Playwright render. Tier 2 (Playwright, in
scraper.py) is only used when the cheap response looks like a JS shell, a
bot challenge, or an outright block.
"""
import ipaddress
from typing import Any, Dict, Optional
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from curl_cffi import CurlOpt
from curl_cffi.requests import AsyncSession

from app.url_safety import UnsafeUrlError, ensure_public_url

# Statuses that usually mean "a real browser might still get through"
ESCALATE_STATUSES = {401, 403, 406, 429, 503}

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

# A rendered page with less visible text than this is probably an SPA shell
MIN_VISIBLE_TEXT = 400
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
MAX_REDIRECTS = 30


async def fetch_http(url: str, timeout_s: int = 20) -> Optional[Dict[str, Any]]:
    """GET a URL with browser TLS fingerprints. Returns None on transport error."""
    try:
        async with AsyncSession(impersonate="chrome", trust_env=False) as session:
            current_url = url
            for redirects in range(MAX_REDIRECTS + 1):
                addresses = await ensure_public_url(current_url)
                if addresses:
                    parsed = urlsplit(current_url)
                    port = parsed.port or (443 if parsed.scheme == "https" else 80)
                    host = parsed.hostname.encode("idna").decode("ascii")
                    try:
                        ipaddress.ip_address(host)
                    except ValueError:
                        pinned = ",".join(
                            f"[{address}]" if ":" in address else address
                            for address in addresses
                        )
                        session.curl_options = {
                            CurlOpt.RESOLVE: [f"{host}:{port}:{pinned}"]
                        }
                    else:
                        # Literal IPs are already pinned by the URL itself, and
                        # IPv6 literals are ambiguous in CURLOPT_RESOLVE syntax.
                        session.curl_options = {}
                resp = await session.get(
                    current_url, timeout=timeout_s, allow_redirects=False
                )
                if addresses:
                    try:
                        connected_ip = str(ipaddress.ip_address(resp.primary_ip))
                    except (TypeError, ValueError) as exc:
                        raise UnsafeUrlError(
                            "Could not verify the connected server address"
                        ) from exc
                    if connected_ip not in addresses:
                        raise UnsafeUrlError(
                            "Connected server address changed after validation"
                        )
                location = resp.headers.get("location")
                if resp.status_code in REDIRECT_STATUSES and location:
                    if redirects == MAX_REDIRECTS:
                        return None
                    current_url = urljoin(current_url, location)
                    continue
                content_type = resp.headers.get("content-type", "")
                return {
                    "status": resp.status_code,
                    "html": resp.text,
                    "content": resp.content,
                    "final_url": str(resp.url),
                    "content_type": content_type,
                }
    except UnsafeUrlError:
        # Do not turn a policy rejection into a transport failure that could
        # trigger a browser-tier retry.
        raise
    except Exception:
        return None


def needs_browser(response: Optional[Dict[str, Any]]) -> bool:
    """Decide whether the cheap HTTP response is good enough or we must render."""
    if response is None:
        return True
    if response["status"] in ESCALATE_STATUSES or response["status"] >= 500:
        return True
    if "html" not in response.get("content_type", "") and response.get("content_type"):
        return True

    html = response.get("html") or ""
    lowered = html[:20000].lower()
    if any(marker in lowered for marker in CHALLENGE_MARKERS):
        return True

    # Visible-text check: empty SPA shells render to almost nothing
    soup = BeautifulSoup(html, "html.parser")
    for el in soup(["script", "style", "noscript", "template"]):
        el.decompose()
    text = soup.get_text(" ", strip=True)
    return len(text) < MIN_VISIBLE_TEXT
