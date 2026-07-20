#!/usr/bin/env python3
"""Collect README/docs trees of major Swift packages via the GitHub API.

For each repo we read its LICENSE (SPDX id) and let that drive the corpus bucket
(license_buckets.source_id_for_repo_license): a permissive license (MIT/Apache/
BSD/…) unlocks RAG + DAPT; anything else stays RAG-only (community-review-
required). We then pull the root README plus the docs/ Markdown tree and write
one crawl artifact per repo under data/crawls/.

Usage:
    python scripts/scrape_github_docs.py                     # curated default repos
    python scripts/scrape_github_docs.py --repo apple/swift-nio --repo vapor/vapor
    GITHUB_TOKEN=... python scripts/scrape_github_docs.py    # higher rate limit
"""
from __future__ import annotations

import argparse
import os
import sys

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import collectors_common as cc
from app.corpus import license_buckets

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"

DEFAULT_REPOS = [
    "apple/swift-nio",
    "vapor/vapor",
    "apple/swift-argument-parser",
    "apple/swift-collections",
    "apple/swift-algorithms",
    "pointfreeco/swift-composable-architecture",
]

_DOC_EXTS = (".md", ".markdown")


def _headers() -> dict:
    h = {"Accept": "application/vnd.github+json",
         "User-Agent": "crawltrove-corpus-collector/1.0"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def select_doc_paths(tree_entries: list, *, max_files: int = 200) -> list:
    """From a git tree, keep the root README and every Markdown file under docs/.

    Pure: `tree_entries` is the GitHub tree API's `tree` list of {path, type}."""
    out = []
    for e in tree_entries or []:
        if e.get("type") != "blob":
            continue
        path = e.get("path") or ""
        low = path.lower()
        is_root_readme = "/" not in path and low.startswith("readme")
        is_doc = low.startswith("docs/") and low.endswith(_DOC_EXTS)
        if is_root_readme or is_doc:
            out.append(path)
        if len(out) >= max_files:
            break
    # README first, then docs alphabetically — stable, readable order.
    out.sort(key=lambda p: (not p.lower().startswith("readme"), p.lower()))
    return out


def repo_license_spdx(repo: str, *, get_json=cc.get_json) -> "str | None":
    try:
        meta = get_json(f"{API}/repos/{repo}/license", headers=_headers())
    except Exception:
        return None
    spdx = ((meta.get("license") or {}).get("spdx_id"))
    # GitHub returns "NOASSERTION" / None for unknown licenses.
    return spdx if spdx and spdx != "NOASSERTION" else None


def collect_repo(repo: str, *, get_json=cc.get_json, get_text=cc.get_text,
                 max_files: int = 200):
    """Return (source_id, results) for one owner/name repo, or (source_id, [])."""
    spdx = repo_license_spdx(repo, get_json=get_json)
    source_id = license_buckets.source_id_for_repo_license(spdx)
    info = get_json(f"{API}/repos/{repo}", headers=_headers())
    branch = info.get("default_branch") or "main"
    tree = get_json(f"{API}/repos/{repo}/git/trees/{branch}?recursive=1",
                    headers=_headers())
    paths = select_doc_paths(tree.get("tree") or [], max_files=max_files)
    results = []
    for path in paths:
        try:
            text = get_text(f"{RAW}/{repo}/{branch}/{path}", headers=_headers())
        except Exception as e:
            print(f"  ! {repo}/{path} failed: {e}")
            continue
        if not text.strip():
            continue
        url = f"https://github.com/{repo}/blob/{branch}/{path}"
        title = f"{repo}: {path}"
        results.append(cc.make_result(url, title, text, repo=repo,
                                      repo_license=spdx, doc_path=path))
    return source_id, results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", action="append", dest="repos",
                    help="owner/name (repeatable); default: curated Swift packages")
    ap.add_argument("--max-files", type=int, default=200)
    args = ap.parse_args()
    repos = args.repos or DEFAULT_REPOS

    total = 0
    for repo in repos:
        print(f"== {repo} ==")
        try:
            source_id, results = collect_repo(repo, max_files=args.max_files)
        except Exception as e:
            print(f"  ! {repo} failed: {e}")
            continue
        if not results:
            print("  (no docs found)")
            continue
        path = cc.write_artifact(results, base_url=f"https://github.com/{repo}",
                                 source=source_id)
        total += len(results)
        print(f"  {len(results)} docs ({source_id}) -> {path}")
    print(f"scrape_github_docs: {total} docs across {len(repos)} repo(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
