#!/usr/bin/env python3
"""Collect permissively-licensed Swift tutorial pages from a curated allowlist
(Epic 3 S5).

Reads scripts/tutorials_allowlist.yaml — each entry asserts a root + its content
license. For every root we scrape pages through the service waterfall and
**confirm the license per page with license_detect** (the scrape already carries
metadata.license): a page whose detected license conflicts with the asserted one
is dropped; matching (or license-silent, allowlist-trusted) pages are kept. Each
root's artifact is tagged with the license-specific source id so build_corpus
routes it correctly (confirmed CC → permissive; otherwise RAG-only).

Writes one crawl artifact per allowlist root under data/crawls/.

Usage:
    python scripts/scrape_tutorials.py
    python scripts/scrape_tutorials.py --config scripts/tutorials_allowlist.yaml --max-pages 100
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import yaml

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import collectors_common as cc
from app.corpus import license_buckets


def _family(license_id):
    if not license_id:
        return None
    lid = license_id.upper()
    if lid.startswith("CC-BY"):
        return "cc-by"
    if lid.startswith("CC0"):
        return "cc0"
    return "other"


def page_passes(expected_license: str, detected_license) -> bool:
    """Keep a page if its detected license matches the asserted family, or if no
    license was detected (allowlist-trusted). Drop on a conflicting license."""
    det = _family(detected_license)
    if det is None:
        return True
    return det == _family(expected_license)


def load_allowlist(path) -> list:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return [s for s in (data.get("sources") or []) if s.get("root")]


def _detected_id(row: dict):
    lic = (row.get("metadata") or {}).get("license") or {}
    return lic.get("id") if isinstance(lic, dict) else None


async def collect_root(root: str, expected_license: str, *, max_pages: int,
                       scrape=cc.default_scrape, discover=None) -> list:
    from app import sitemap
    if discover is not None:
        urls = await discover(root, max_pages)
    else:
        try:
            urls = (await sitemap.map_site(root, limit=max_pages)) or []
        except Exception:
            urls = []
        urls = [root] + urls
    seen, kept = set(), []
    for u in urls:
        u = (u or "").split("#", 1)[0].rstrip("/")
        if not u or u in seen:
            continue
        seen.add(u)
        row = await scrape(u)
        if row is None:
            continue
        if page_passes(expected_license, _detected_id(row)):
            kept.append(row)
        if len(kept) >= max_pages:
            break
    return kept


def main() -> int:
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.path.join(repo, "scripts", "tutorials_allowlist.yaml"))
    ap.add_argument("--max-pages", type=int, default=100)
    args = ap.parse_args()

    sources = load_allowlist(args.config)
    if not sources:
        print(f"no sources in {args.config}")
        return 1
    total = 0
    for s in sources:
        root, expected = s["root"], s.get("expected_license", "")
        source_id = license_buckets.source_id_for_detected_license(expected)
        print(f"== {s.get('name', root)} ({expected} -> {source_id}) ==")
        results = asyncio.run(collect_root(root, expected, max_pages=args.max_pages))
        if not results:
            print("  (no pages passed the license gate)")
            continue
        path = cc.write_artifact(results, base_url=root, source=source_id)
        total += len(results)
        print(f"  {len(results)} pages -> {path}")
    print(f"scrape_tutorials: {total} pages across {len(sources)} source(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
