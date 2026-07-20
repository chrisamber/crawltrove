#!/usr/bin/env python3
"""Collect swift.org blog + documentation pages.

swift.org content is Apache-2.0 (permissive → RAG + DAPT-eligible; see
license_buckets.py source 'swiftorg'). Discovers content URLs from the site's
sitemap (falling back to the given seed roots), scrapes each through the service
waterfall, and writes a crawl artifact (source=swiftorg).

Usage:
    python scripts/scrape_swiftorg.py                       # blog + docs defaults
    python scripts/scrape_swiftorg.py --root https://www.swift.org/documentation/ --max-pages 200
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from urllib.parse import urlsplit

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import collectors_common as cc

DEFAULT_ROOTS = [
    "https://www.swift.org/blog/",
    "https://www.swift.org/documentation/",
    "https://www.swift.org/getting-started/",
]

# Non-content paths on swift.org we never want as corpus pages.
_SKIP_PREFIXES = ("/assets/", "/apple-touch", "/feed", "/atom", "/sitemap")
_SKIP_SUFFIXES = (".xml", ".json", ".png", ".jpg", ".svg", ".ico", ".css", ".js")


def is_content_url(url: str) -> bool:
    """Keep same-site swift.org HTML content; drop assets/feeds/off-site links."""
    parts = urlsplit(url)
    host = parts.netloc.lower()
    if "swift.org" not in host:
        return False
    path = parts.path.lower()
    if any(path.startswith(p) for p in _SKIP_PREFIXES):
        return False
    if any(path.endswith(s) for s in _SKIP_SUFFIXES):
        return False
    return True


def select_urls(candidate_urls, *, max_pages: int) -> list:
    """De-dup + filter discovered URLs to content pages, capped."""
    seen, out = set(), []
    for u in candidate_urls:
        u = (u or "").split("#", 1)[0].rstrip("/")
        if not u or u in seen or not is_content_url(u):
            continue
        seen.add(u)
        out.append(u)
        if len(out) >= max_pages:
            break
    return out


async def _discover(roots, max_pages: int) -> list:
    from app import sitemap
    found: list = []
    for root in roots:
        try:
            links = await sitemap.map_site(root, limit=max_pages)
        except Exception as e:
            print(f"  ! discovery failed for {root}: {e}")
            links = []
        found.extend(links or [])
        found.append(root)
    return select_urls(found, max_pages=max_pages)


async def collect(roots, *, max_pages: int, scrape=cc.default_scrape,
                  discover=None) -> list:
    urls = await (discover(roots, max_pages) if discover else _discover(roots, max_pages))
    results = []
    for u in urls:
        row = await scrape(u)
        if row is not None:
            results.append(row)
            print(f"  + {u}")
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", action="append", dest="roots",
                    help="seed root (repeatable); default: blog + docs + getting-started")
    ap.add_argument("--max-pages", type=int, default=150)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    roots = args.roots or DEFAULT_ROOTS

    results = asyncio.run(collect(roots, max_pages=args.max_pages))
    if not results:
        print("no pages collected")
        return 1
    path = cc.write_artifact(results, base_url=roots[0], source="swiftorg", out=args.out)
    print(f"scrape_swiftorg: {len(results)} pages -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
