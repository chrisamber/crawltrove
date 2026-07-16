"""Sitemap-first URL discovery for the crawler.

Frontier crawlers seed from
sitemaps instead of relying purely on link-walking — fuller coverage of a
site in fewer fetches. We read Sitemap: lines from robots.txt, fall back to
the conventional /sitemap.xml locations, and follow one level of sitemap
index nesting. Best-effort throughout: any failure just means the crawler
falls back to plain link discovery.
"""
import re
from typing import List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app import fetch
from app.normalize import normalize_url

LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.IGNORECASE | re.DOTALL)
MAX_SITEMAP_FETCHES = 8


async def _get(url: str) -> Optional[str]:
    resp = await fetch.fetch_http(url, timeout_s=15)
    if resp is None or resp["status"] != 200:
        return None
    return resp["html"]


def _locs(xml: str) -> List[str]:
    return [loc.strip() for loc in LOC_RE.findall(xml) if loc.strip()]


async def discover(base_url: str, cap: int = 200) -> List[str]:
    """Return up to cap same-domain page URLs discovered via sitemaps."""
    parsed = urlparse(base_url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    domain = parsed.netloc.lower()

    sitemap_urls: List[str] = []
    robots = await _get(f"{root}/robots.txt")
    if robots:
        for line in robots.splitlines():
            if line.lower().startswith("sitemap:"):
                sitemap_urls.append(line.split(":", 1)[1].strip())
    if not sitemap_urls:
        sitemap_urls = [f"{root}/sitemap.xml", f"{root}/sitemap_index.xml"]

    pages: List[str] = []
    seen = set()
    fetches = 0
    queue = list(sitemap_urls)
    while queue and fetches < MAX_SITEMAP_FETCHES and len(pages) < cap:
        sm_url = queue.pop(0)
        if sm_url in seen or sm_url.endswith(".gz"):
            seen.add(sm_url)
            continue
        seen.add(sm_url)
        fetches += 1
        xml = await _get(sm_url)
        if not xml:
            continue
        for loc in _locs(xml):
            if urlparse(loc).netloc.lower() not in (domain, ""):
                continue
            # A <loc> ending in .xml inside a sitemapindex is a nested sitemap
            if loc.lower().rstrip("/").endswith(".xml"):
                queue.append(loc)
            elif loc not in pages:
                pages.append(loc)
                if len(pages) >= cap:
                    break
    return pages


def _page_links(html: str, base_url: str, base_domain: str) -> List[str]:
    """Same-domain/subdomain links on one page (the /api/map shallow pass).

    Deliberately a local copy of the crawler's link filter rather than a
    refactor of WebCrawler._extract_links — /api/map must not couple the
    crawler's hot path to this endpoint.
    """
    soup = BeautifulSoup(html, "html.parser")
    links: List[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        full_url = urljoin(base_url, href)
        target = urlparse(full_url).netloc.lower()
        ref = base_domain.lower()
        if target == ref or target.endswith("." + ref):
            links.append(full_url)
    return links


async def map_site(base_url: str, *, limit: int = 100,
                   search: Optional[str] = None,
                   sitemap_only: bool = False) -> List[str]:
    """Fast URL discovery for POST /api/map.

    Merges sitemap discovery with one shallow tier-1 fetch of the base page
    (skipped with sitemap_only), dedups on the shared URL identity, applies an
    optional case-insensitive substring filter, and caps at `limit`. Best-effort
    throughout — any fetch failure just yields fewer links, never an error.
    """
    base_domain = urlparse(base_url).netloc

    candidates: List[str] = [base_url]
    candidates += await discover(base_url, cap=limit)
    if not sitemap_only:
        try:
            resp = await fetch.fetch_http(base_url)
            if resp and resp["status"] == 200 and resp.get("html"):
                candidates += _page_links(resp["html"], base_url, base_domain)
        except Exception:
            pass

    needle = (search or "").lower()
    links: List[str] = []
    seen = set()
    for url in candidates:
        norm = normalize_url(url)
        if norm in seen:
            continue
        seen.add(norm)
        if needle and needle not in url.lower():
            continue
        links.append(url)
        if len(links) >= limit:
            break
    return links
