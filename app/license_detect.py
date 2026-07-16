"""Scrape-time license detection (the Common Pile lesson: tag at the source).

Checks, in order of reliability:
  1. <link rel="license"> / <a rel="license"> hrefs
  2. schema.org JSON-LD "license" fields
  3. <meta name="dcterms.license"> / <meta name="dc.rights">
  4. Creative Commons URLs or "CC BY-SA 4.0"-style strings anywhere in the page

Runs on the RAW html (before cleaning) because license markers usually live in
the footer — exactly what the main-content cleaners strip out.

Caveat from the Common Pile paper: a page-level license is not proof the page
*content* is freely licensed ("license laundering"); treat the result as a
candidate tag to verify per-domain, not ground truth.
"""
import json
import re
from typing import Any, Dict, Optional

from bs4 import BeautifulSoup

CC_URL_RE = re.compile(
    r"creativecommons\.org/(?:licenses/(?P<code>[a-z-]+)/(?P<ver>\d\.\d)"
    r"|publicdomain/(?P<pd>zero|mark)/(?P<pdver>\d\.\d))",
    re.IGNORECASE,
)
CC_TEXT_RE = re.compile(
    r"\bCC[ -](?P<code>BY(?:[ -](?:SA|NC|ND|NC[ -]SA|NC[ -]ND))?|0)"
    r"(?:[ -](?P<ver>\d\.\d))?\b"
)


def _normalize_cc_url(url: str) -> Optional[str]:
    m = CC_URL_RE.search(url)
    if not m:
        return None
    if m.group("pd"):
        return "CC0-" + m.group("pdver") if m.group("pd") == "zero" else "CC-PDM"
    return f"CC-{m.group('code').upper()}-{m.group('ver')}"


def _normalize_cc_text(text: str) -> Optional[str]:
    m = CC_TEXT_RE.search(text)
    if not m:
        return None
    code = m.group("code").replace(" ", "-").upper()
    if code == "0":
        return "CC0-1.0"
    ver = m.group("ver") or ""
    return f"CC-{code}-{ver}" if ver else f"CC-{code}"


def _result(license_id: str, url: str, source: str, evidence: str) -> Dict[str, Any]:
    return {
        "id": license_id,
        "url": url,
        "source": source,
        "evidence": evidence[:300],
    }


def _jsonld_licenses(soup: BeautifulSoup):
    """Yield license values from any JSON-LD block, however nested."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if "license" in node:
                    yield node["license"]
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)


def detect_text(text: str) -> Optional[Dict[str, Any]]:
    """License detection for plain text/markdown (e.g. PDF content) — CC markers only."""
    m = CC_URL_RE.search(text)
    if m:
        return _result(_normalize_cc_url(m.group(0)) or "unknown", m.group(0), "cc-url",
                       text[max(0, m.start() - 30): m.end()])
    license_id = _normalize_cc_text(text)
    if license_id:
        m = CC_TEXT_RE.search(text)
        return _result(license_id, "", "cc-text", text[max(0, m.start() - 60): m.end() + 60])
    return None


def detect(html: str) -> Optional[Dict[str, Any]]:
    """Return {id, url, source, evidence} for the strongest license signal, or None."""
    soup = BeautifulSoup(html, "html.parser")

    # 1. rel="license" links — the machine-readable convention
    for tag in soup.find_all(["link", "a"], rel=True):
        rels = tag.get("rel") or []
        if "license" in [r.lower() for r in rels]:
            href = tag.get("href", "")
            license_id = _normalize_cc_url(href)
            if license_id or href:
                return _result(license_id or "unknown", href, "rel-license", str(tag))

    # 2. schema.org JSON-LD
    for value in _jsonld_licenses(soup):
        if isinstance(value, dict):
            value = value.get("url") or value.get("@id") or ""
        if isinstance(value, str) and value:
            license_id = _normalize_cc_url(value) or _normalize_cc_text(value)
            return _result(license_id or "unknown", value, "json-ld", value)

    # 3. Dublin Core meta tags
    for name in ("dcterms.license", "dc.rights", "rights"):
        meta = soup.find("meta", attrs={"name": name})
        if meta and meta.get("content"):
            content = meta["content"]
            license_id = _normalize_cc_url(content) or _normalize_cc_text(content)
            return _result(license_id or "unknown", content, "meta", content)

    # 4. CC URL or license string anywhere in the page (usually the footer)
    m = CC_URL_RE.search(html)
    if m:
        url = html[max(0, m.start() - 30): m.end()]
        return _result(_normalize_cc_url(m.group(0)) or "unknown", m.group(0), "cc-url", url)

    visible = soup.get_text(" ", strip=True)
    license_id = _normalize_cc_text(visible)
    if license_id:
        m = CC_TEXT_RE.search(visible)
        ctx = visible[max(0, m.start() - 60): m.end() + 60]
        return _result(license_id, "", "cc-text", ctx)

    return None
