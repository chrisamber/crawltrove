#!/usr/bin/env python3
"""Build a DAPT-scale corpus from a set of Apple framework documentation trees.

Wraps the single-root collector (scrape_apple_docs.py) to crawl many frameworks
and package the result for domain-adaptive pretraining (DAPT) / continued
pretraining.

Two phases:
  1. CRAWL  — for each framework root /documentation/<fw>, BFS its render-JSON
     subtree, convert to markdown, run corpus signals, and save a per-framework
     crawl artifact (data/crawls/...). A marker (data/dapt/.done/<fw>.json) makes
     the run RESUMABLE: re-running skips frameworks already crawled. The dedup
     index is shared/persistent, so near/exact duplicates are flagged ACROSS
     frameworks (e.g. an enum case repeated in two frameworks).
  2. PACKAGE — read the per-framework artifacts and emit one DAPT file
     (data/dapt/<name>.jsonl, one {text, meta} record per page), dropping EXACT
     duplicates by content_hash (standard for pretraining) while keeping near-dups
     flagged in meta. Writes a stats summary alongside.

Usage (from repo root, with the project venv):
    .venv/bin/python scripts/build_dapt_corpus.py --name avfoundation-audio
    .venv/bin/python scripts/build_dapt_corpus.py --package-only --name avfoundation-audio
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from collections import Counter
from typing import Any, Dict, List

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import scrape_apple_docs as col  # same dir; inserts repo root for `app` on import

# The "Audio + AVFoundation" bundle.
AUDIO_FRAMEWORKS = [
    "avfaudio", "audiotoolbox", "coreaudio", "coreaudiotypes", "coreaudiokit",
    "coremidi", "phase", "audiodriverkit", "audiounit", "soundanalysis", "shazamkit",
]
DEFAULT_FRAMEWORKS = AUDIO_FRAMEWORKS + ["avfoundation"]

DAPT_DIR = os.path.join(_REPO_ROOT, "data", "dapt")
DONE_DIR = os.path.join(DAPT_DIR, ".done")


def _est_tokens(chars: int) -> int:
    return chars // 4  # rough; ~4 chars/token for English+code


def crawl_framework(fw: str, concurrency: int, max_pages: int, max_depth: int,
                    force: bool) -> Dict[str, Any]:
    """Crawl one framework subtree, save its artifact, return a marker summary."""
    os.makedirs(DONE_DIR, exist_ok=True)
    marker_path = os.path.join(DONE_DIR, f"{fw}.json")
    if os.path.exists(marker_path) and not force:
        with open(marker_path, encoding="utf-8") as f:
            m = json.load(f)
        print(f"[{fw}] already done -> {m['pages']} pages (artifact {m['stem']})", flush=True)
        return m

    from app import dedup, storage

    root_path = f"/documentation/{fw}"
    scope_prefix = f"/documentation/{fw}/"
    print(f"[{fw}] crawling {root_path} ...", flush=True)
    docs, failed = col.crawl_graph(root_path, scope_prefix, max_pages, max_depth, concurrency)

    ordered = sorted(docs.items(), key=lambda kv: (kv[0] != root_path, kv[0]))
    results: List[Dict[str, Any]] = []
    for p, doc in ordered:
        res = col.build_result(p, doc, run_signals=True)
        res["metadata"]["framework"] = fw
        res["metadata"]["dedup"] = dedup.check_and_register(res["markdown"], res["url"])
        results.append(res)

    now = datetime.datetime.now().isoformat(timespec="seconds")
    job = {
        "jobId": None,
        "base_url": col.doc_url_to_page_url(root_path),
        "status": "completed",
        "engine": "http-json",
        "source": "appledocs-docc",
        "framework": fw,
        "scope_prefix": scope_prefix,
        "total": len(results),
        "skipped": [col.doc_url_to_page_url(p) for p in failed],
        "start_time": now,
        "end_time": now,
        "results": results,
    }
    stem = storage.save_crawl(job)
    chars = sum(len(r["markdown"]) for r in results)
    m = {
        "framework": fw,
        "stem": stem,
        "pages": len(results),
        "skipped": len(failed),
        "chars": chars,
        "est_tokens": _est_tokens(chars),
        "crawled_at": now,
    }
    with open(marker_path, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=2)
    print(f"[{fw}] done -> {len(results)} pages, {len(failed)} skipped, "
          f"~{_est_tokens(chars):,} tokens (artifact {stem})", flush=True)
    return m


def package(name: str, markers: List[Dict[str, Any]], drop_exact: bool) -> Dict[str, Any]:
    """Read per-framework artifacts -> one DAPT JSONL, exact-deduped."""
    os.makedirs(DAPT_DIR, exist_ok=True)
    out_jsonl = os.path.join(DAPT_DIR, f"{name}.jsonl")

    seen_hash: set = set()
    n_written = 0
    n_exact_dropped = 0
    n_empty = 0
    total_chars = 0
    per_fw = Counter()
    kinds = Counter()
    langs = Counter()
    near_dup = 0
    quality_pass = 0

    with open(out_jsonl, "w", encoding="utf-8") as out:
        for m in markers:
            apath = os.path.join(_REPO_ROOT, "data", "crawls", m["stem"] + ".json")
            with open(apath, encoding="utf-8") as f:
                job = json.load(f)
            for r in job.get("results", []):
                text = r.get("markdown", "") or ""
                if not text.strip():
                    n_empty += 1
                    continue
                meta = r.get("metadata", {})
                dd = meta.get("dedup") or {}
                chash = dd.get("content_hash", "")
                if drop_exact and chash and chash in seen_hash:
                    n_exact_dropped += 1
                    continue
                if chash:
                    seen_hash.add(chash)
                rec = {
                    "text": text,
                    "meta": {
                        "url": r.get("url", ""),
                        "title": r.get("title", ""),
                        "framework": meta.get("framework", m["framework"]),
                        "symbolKind": meta.get("symbolKind", ""),
                        "roleHeading": meta.get("roleHeading", ""),
                        "language": (meta.get("language") or {}).get("lang", ""),
                        "quality_passed": (meta.get("quality") or {}).get("passed"),
                        "content_hash": chash,
                        "near_duplicate_of": dd.get("near_duplicate_of"),
                    },
                }
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n_written += 1
                total_chars += len(text)
                per_fw[meta.get("framework", m["framework"])] += 1
                kinds[meta.get("symbolKind") or meta.get("roleHeading") or "?"] += 1
                langs[(meta.get("language") or {}).get("lang", "?")] += 1
                if dd.get("near_duplicate_of"):
                    near_dup += 1
                if (meta.get("quality") or {}).get("passed"):
                    quality_pass += 1

    stats = {
        "name": name,
        "jsonl": os.path.relpath(out_jsonl, _REPO_ROOT),
        "records": n_written,
        "exact_dups_dropped": n_exact_dropped,
        "empty_skipped": n_empty,
        "total_chars": total_chars,
        "est_tokens": _est_tokens(total_chars),
        "near_dups_flagged": near_dup,
        "quality_pass": quality_pass,
        "quality_pass_pct": round(100 * quality_pass / n_written, 1) if n_written else 0,
        "per_framework": dict(per_fw.most_common()),
        "per_framework_skipped": {m["framework"]: m["skipped"] for m in markers},
        "languages": dict(langs.most_common()),
        "page_kinds": dict(kinds.most_common()),
    }
    with open(os.path.join(DAPT_DIR, f"{name}.stats.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2)
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--name", default="avfoundation-audio")
    ap.add_argument("--frameworks", default=",".join(DEFAULT_FRAMEWORKS))
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--max-pages", type=int, default=15000)
    ap.add_argument("--max-depth", type=int, default=8)
    ap.add_argument("--force", action="store_true", help="re-crawl even if marker exists")
    ap.add_argument("--package-only", action="store_true", help="skip crawling; package existing markers")
    ap.add_argument("--no-drop-exact", action="store_true", help="keep exact dups in the JSONL")
    args = ap.parse_args()

    fws = [f.strip() for f in args.frameworks.split(",") if f.strip()]
    print(f"DAPT corpus '{args.name}' over {len(fws)} frameworks: {fws}\n", flush=True)

    markers: List[Dict[str, Any]] = []
    for fw in fws:
        if args.package_only:
            mp = os.path.join(DONE_DIR, f"{fw}.json")
            if not os.path.exists(mp):
                print(f"[{fw}] no marker; skipping in package-only mode", flush=True)
                continue
            with open(mp, encoding="utf-8") as f:
                markers.append(json.load(f))
        else:
            markers.append(crawl_framework(
                fw, args.concurrency, args.max_pages, args.max_depth, args.force))

    crawled_pages = sum(m["pages"] for m in markers)
    crawled_tokens = sum(m["est_tokens"] for m in markers)
    print(f"\n=== crawl totals ===\npages: {crawled_pages:,}  ~tokens: {crawled_tokens:,}", flush=True)

    print("\npackaging DAPT JSONL ...", flush=True)
    stats = package(args.name, markers, drop_exact=not args.no_drop_exact)

    print("\n=== DAPT corpus report ===")
    print(f"file:               {stats['jsonl']}")
    print(f"records:            {stats['records']:,}")
    print(f"exact dups dropped: {stats['exact_dups_dropped']:,}")
    print(f"near dups flagged:  {stats['near_dups_flagged']:,}")
    print(f"est. tokens:        {stats['est_tokens']:,}")
    print(f"quality pass:       {stats['quality_pass']:,} ({stats['quality_pass_pct']}%)")
    print(f"languages:          {stats['languages']}")
    print(f"per framework:      {stats['per_framework']}")
    print(f"skipped (403/etc):  {stats['per_framework_skipped']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
