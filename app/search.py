"""Web search providers for research runs.

Provider selection by env: SEARXNG_BASE_URL wins, then BRAVE_SEARCH_API_KEY,
else the keyless DuckDuckGo HTML endpoint (always available, so search is
never "unconfigured"). Failures degrade to an empty result list — a dead
provider never crashes a research run (resilient-signal convention).
"""
import logging
import os
from typing import Any, Dict, List
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger("search")


def provider() -> str:
    if os.environ.get("SEARXNG_BASE_URL"):
        return "searxng"
    if os.environ.get("BRAVE_SEARCH_API_KEY"):
        return "brave"
    return "ddg"


def parse_searxng(body: Dict[str, Any], n: int) -> List[Dict[str, str]]:
    out = []
    for r in (body.get("results") or []):
        if len(out) >= n:
            break
        if r.get("url"):
            out.append({"url": r["url"], "title": r.get("title") or "",
                        "snippet": r.get("content") or ""})
    return out


def parse_brave(body: Dict[str, Any], n: int) -> List[Dict[str, str]]:
    results = ((body.get("web") or {}).get("results") or [])
    out = []
    for r in results:
        if len(out) >= n:
            break
        if r.get("url"):
            out.append({"url": r["url"], "title": r.get("title") or "",
                        "snippet": r.get("description") or ""})
    return out


def parse_ddg(html: str, n: int) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    out: List[Dict[str, str]] = []
    for a in soup.select("a.result__a"):
        if len(out) >= n:
            break
        href = a.get("href") or ""
        # DDG wraps result links in a redirect: //duckduckgo.com/l/?uddg=<url>&...
        if "uddg=" in href:
            href = unquote(parse_qs(urlparse(href).query).get("uddg", [""])[0])
        if not href.startswith("http"):
            continue
        snippet = ""
        body = a.find_parent(class_="result__body")
        if body:
            sn = body.select_one(".result__snippet")
            snippet = sn.get_text(" ", strip=True) if sn else ""
        out.append({"url": href, "title": a.get_text(" ", strip=True),
                    "snippet": snippet})
    return out


async def search(query: str, n: int = 8) -> List[Dict[str, str]]:
    """One web search via the configured provider. [] on any failure."""
    which = provider()
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as http:
            if which == "searxng":
                base = os.environ["SEARXNG_BASE_URL"].rstrip("/")
                resp = await http.get(f"{base}/search",
                                      params={"q": query, "format": "json"})
                resp.raise_for_status()
                return parse_searxng(resp.json(), n)
            if which == "brave":
                resp = await http.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    params={"q": query, "count": n},
                    headers={"X-Subscription-Token":
                             os.environ["BRAVE_SEARCH_API_KEY"]})
                resp.raise_for_status()
                return parse_brave(resp.json(), n)
            resp = await http.get("https://html.duckduckgo.com/html/",
                                  params={"q": query},
                                  headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            return parse_ddg(resp.text, n)
    except Exception as e:
        logger.warning("search failed (%s, %r): %s", which, query, e)
        return []
