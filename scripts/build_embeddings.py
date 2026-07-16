#!/usr/bin/env python3
"""Backfill / reindex the semantic vector index (Epic 3 S1).

Walks the file artifacts the system produces and indexes them into
``data/index/vectors.db`` via ``app.vecindex``:

  * data/scrapes/*.json        → kind=scrape   (ref = stem)
  * data/crawls/*.json         → kind=crawl    (ref = stem#<pageIndex>, one per page)
  * data/research/*.json       → kind=research (ref = stem, the report)
  * data/corpus/rag/**/*.jsonl → kind=corpus   (ref = record id / content_hash)

Resumable: a document already present in the index is skipped unless
``--reindex`` is given (which wipes the DB and rebuilds from scratch — the
recovery path when the embedding model/dimension changes).

Requires an embedding backend (EMBEDDINGS_BASE_URL + EMBEDDINGS_MODEL); prints a
message and exits non-zero otherwise. Reads files only — never mutates them.

Usage:
    python scripts/build_embeddings.py                 # backfill everything new
    python scripts/build_embeddings.py --reindex       # wipe + rebuild
    python scripts/build_embeddings.py --kind corpus   # one kind only
    python scripts/build_embeddings.py --limit 100      # cap docs (smoke test)
"""
import argparse
import asyncio
import glob
import json
import os
import sys

# Allow running as a bare script (python scripts/build_embeddings.py).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import embeddings, storage, vecindex  # noqa: E402

KINDS = ("scrape", "crawl", "research", "corpus")


def _iter_json(folder):
    if not os.path.isdir(folder):
        return
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(folder, name)
        try:
            with open(path, encoding="utf-8") as f:
                yield name[:-5], json.load(f)
        except Exception as e:
            print(f"  ! skip unreadable {path}: {e}")


def _scrape_docs():
    for stem, data in _iter_json(storage.SCRAPES_DIR):
        yield {"kind": "scrape", "ref": stem, "url": data.get("url"),
               "text": data.get("markdown", ""),
               "meta": {"title": data.get("title"), "url": data.get("url")}}


def _crawl_docs():
    for stem, data in _iter_json(storage.CRAWLS_DIR):
        for idx, item in enumerate(data.get("results", []) or []):
            yield {"kind": "crawl", "ref": f"{stem}#{idx}", "url": item.get("url"),
                   "text": item.get("markdown", ""),
                   "meta": {"title": item.get("title"), "url": item.get("url"),
                            "crawl": stem}}


def _research_docs():
    for stem, data in _iter_json(storage.RESEARCH_DIR):
        if not data.get("report"):
            continue
        yield {"kind": "research", "ref": stem, "url": None,
               "text": data.get("report", ""),
               "meta": {"query": data.get("query"), "stem": stem}}


def _corpus_docs():
    root = os.path.join(storage.DATA_DIR, "corpus", "rag")
    for path in sorted(glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)):
        rel = os.path.relpath(path, storage.DATA_DIR)
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    ref = rec.get("id") or rec.get("content_hash")
                    if not ref or not rec.get("text"):
                        continue
                    yield {"kind": "corpus", "ref": ref, "url": rec.get("url"),
                           "text": rec.get("text", ""),
                           "meta": {"title": rec.get("title"), "url": rec.get("url"),
                                    "namespace": rec.get("namespace"),
                                    "framework": rec.get("framework"),
                                    "license_bucket": rec.get("license_bucket"),
                                    "quality_tier": rec.get("quality_tier"),
                                    "parent_hash": rec.get("parent_hash"),
                                    "chunk_index": rec.get("chunk_index"),
                                    "heading_path": rec.get("heading_path"),
                                    "file": rel}}
        except Exception as e:
            print(f"  ! skip unreadable {path}: {e}")


_SOURCES = {"scrape": _scrape_docs, "crawl": _crawl_docs,
            "research": _research_docs, "corpus": _corpus_docs}


async def run(kinds, reindex, limit):
    if not embeddings.configured():
        print("No embedding backend configured. Set EMBEDDINGS_BASE_URL "
              "(+ EMBEDDINGS_MODEL) and retry.", file=sys.stderr)
        return 1

    if reindex:
        try:
            if os.path.exists(vecindex.DB_PATH):
                os.remove(vecindex.DB_PATH)
                print(f"removed existing index {vecindex.DB_PATH}")
        except OSError as e:
            print(f"could not remove index: {e}", file=sys.stderr)
            return 1
        vecindex._reset_for_tests()  # drop any cached connection

    if not vecindex.available():
        print("sqlite-vec extension unavailable — cannot build the index.",
              file=sys.stderr)
        return 1

    total_docs = total_chunks = skipped = 0
    for kind in kinds:
        print(f"== {kind} ==")
        for doc in _SOURCES[kind]():
            if limit and total_docs >= limit:
                break
            if not reindex and vecindex.ref_indexed(doc["kind"], doc["ref"]):
                skipped += 1
                continue
            n = await vecindex.index_document(
                doc["kind"], doc["ref"], doc["url"], doc["text"], meta=doc["meta"])
            total_docs += 1
            total_chunks += n
            if n:
                print(f"  + {doc['ref']}  ({n} chunks)")
            else:
                print(f"  · {doc['ref']}  (no chunks / backend down)")
        if limit and total_docs >= limit:
            print(f"(stopped at --limit {limit})")
            break

    print(f"\nindexed {total_docs} document(s), {total_chunks} chunk(s); "
          f"skipped {skipped} already-indexed")
    print("index stats:", json.dumps(vecindex.stats()))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kind", choices=KINDS, action="append",
                    help="Limit to one kind (repeatable). Default: all.")
    ap.add_argument("--reindex", action="store_true",
                    help="Wipe the index and rebuild from scratch (dimension recovery).")
    ap.add_argument("--limit", type=int, default=0,
                    help="Cap the number of documents indexed (0 = no cap).")
    args = ap.parse_args()
    kinds = args.kind or list(KINDS)
    sys.exit(asyncio.run(run(kinds, args.reindex, args.limit)))


if __name__ == "__main__":
    main()
