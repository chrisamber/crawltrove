"""Build routed corpus records from a saved crawl artifact (simplified).

Reads a storage.save_crawl job JSON, turns each crawl result into one coarse
corpus record (markdown as text + light metadata), routes it by license bucket,
and writes corpus/<target>/<namespace>/<framework>.jsonl under DATA_DIR.

This is the minimal record builder: no symbol-card / chunker / deep-availability
parsing (a later Phase enriches this). It exists so the Swift 6.4 / iOS 27 scrape
loop can produce RAG/SFT-ready output today.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.corpus import (chunking, layout, license_buckets, namespaces,
                        provenance, quality_tiers, router, schema)


def availability_from_platforms(platforms) -> Dict[str, Any]:
    introduced = None
    for p in platforms or []:
        if p.get("introducedAt"):
            introduced = f"{p.get('name')} {p['introducedAt']}"
            break
    return {"introduced": introduced, "deprecated": None, "beta": False}


def framework_from_url(url: str) -> str:
    parts = urlsplit(url or "")
    if "developer.apple.com" not in parts.netloc.lower():
        return ""
    segs = parts.path.strip("/").split("/")
    if len(segs) >= 2 and segs[0] == "documentation":
        return segs[1].lower()
    return ""


def content_hash_for(result: Dict[str, Any]) -> str:
    dedup = (result.get("metadata") or {}).get("dedup") or {}
    h = dedup.get("content_hash")
    if h:
        return h
    md = result.get("markdown") or ""
    return "sha256:" + hashlib.sha256(md.encode("utf-8")).hexdigest()


def build_record(result: Dict[str, Any], source: str, scraped_at: str = "",
                 tier_overrides: Dict[str, Any] = None) -> Dict[str, Any]:
    meta = result.get("metadata") or {}
    url = result.get("url", "")
    ch = content_hash_for(result)
    tier = quality_tiers.tier_for(meta.get("quality"), **(tier_overrides or {}))
    return schema.new_record(
        id=ch,
        source=source,
        url=url,
        title=meta.get("title", ""),
        symbol=meta.get("title", ""),
        symbol_kind=meta.get("symbolKind") or meta.get("roleHeading", ""),
        framework=framework_from_url(url),
        platforms=meta.get("platforms", []) or [],
        availability=availability_from_platforms(meta.get("platforms")),
        swift_version="",
        xcode_version="",
        scraped_at=scraped_at,
        license_bucket=license_buckets.bucket_for(source),
        content_hash=ch,
        chunk_type="symbol_card",
        namespace=namespaces.namespace_for(url, source),
        text=result.get("markdown", ""),
        quality_tier=tier,
    )


def records_from_crawl(artifact_path, source: str, scraped_at: str = "",
                       tier_overrides: Dict[str, Any] = None) -> List[Dict[str, Any]]:
    """One page-level record per crawl result (the base record; RAG chunking and
    routing happen in write_records)."""
    job = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
    return [build_record(r, source, scraped_at, tier_overrides)
            for r in job.get("results", [])]


def chunk_records(page_rec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Explode a page record into one record per structure-aware chunk (Epic 3
    S2). Each chunk gets its own content_hash/id, a parent_hash pointing at the
    page, a chunk_index, and a heading-path breadcrumb; all other metadata is
    inherited. A page that chunks to nothing degrades to a single whole-page
    chunk so no content is lost."""
    parent = page_rec.get("content_hash") or page_rec.get("id") or ""
    chunks = chunking.chunk_markdown(page_rec.get("text", ""))
    if not chunks:
        chunks = [{"text": page_rec.get("text", ""), "heading_path": [], "chunk_index": 0}]
    out: List[Dict[str, Any]] = []
    for c in chunks:
        ch = "sha256:" + hashlib.sha256(c["text"].encode("utf-8")).hexdigest()
        rec = dict(page_rec)
        rec["text"] = c["text"]
        rec["chunk_index"] = c["chunk_index"]
        rec["heading_path"] = c["heading_path"]
        rec["parent_hash"] = parent
        rec["content_hash"] = ch
        rec["id"] = ch
        out.append(rec)
    return out


def _records_for_target(page_rec: Dict[str, Any], target: str) -> List[Dict[str, Any]]:
    """RAG gets one record per chunk; DAPT/SFT keep the whole-page record
    (packing handles length in DAPT; SFT generation consumes chunks separately)."""
    if target == "rag":
        return chunk_records(page_rec)
    return [page_rec]


def write_records(records: List[Dict[str, Any]], base, source: str,
                  route_kwargs: Dict[str, Any] = None,
                  corpus_dir=None, skip_known: bool = True,
                  record_provenance: bool = True) -> Dict[str, Any]:
    """Route + write records under ``base``.

    Normal (incremental) mode uses the append-only content-hash ledger to skip
    already-seen records and records provenance for new ones. The rebuild path
    (Epic 3 S6) overrides:
      * ``corpus_dir`` — write corpus/<target>/... under this root instead of
        ``base/corpus`` (so a rebuild can stage into data/corpus.new/), while
        metadata ledgers stay under ``base/metadata``.
      * ``skip_known=False`` — re-emit every record (a rebuild regenerates the
        whole corpus from scratch), deduping only within this run.
      * ``record_provenance=False`` — don't re-append content-hash/source rows
        (the ledger is append-only history, not rewritten by a rebuild).
    """
    paths = layout.ensure_layout(base)
    meta_dir = paths["metadata"]
    base = Path(base)
    corpus_root = Path(corpus_dir) if corpus_dir else base / "corpus"
    known = set(provenance.load_content_hashes(meta_dir)) if skip_known else set()
    written: Dict[str, int] = {}
    by_namespace: Dict[str, int] = {}
    by_tier: Dict[str, int] = {}
    unchanged = 0
    no_target = 0
    seen: set = set()
    for page_rec in records:
        targets = router.route(page_rec, **(route_kwargs or {}))
        if not targets:
            no_target += 1
            continue
        counted_ns = False
        for target in sorted(targets):
            for rec in _records_for_target(page_rec, target):
                ch = rec["content_hash"]
                if ch in known or ch in seen:
                    unchanged += 1
                    continue
                seen.add(ch)
                ns = rec["namespace"]
                fw = rec["framework"] or "general"
                out = corpus_root / target / ns / f"{fw}.jsonl"
                out.parent.mkdir(parents=True, exist_ok=True)
                with open(out, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                written[target] = written.get(target, 0) + 1
                tier = rec.get("quality_tier") or "untiered"
                by_tier[tier] = by_tier.get(tier, 0) + 1
                if record_provenance:
                    provenance.record_content_hash(meta_dir, ch, {
                        "url": rec["url"], "source": source,
                        "quality_tier": rec.get("quality_tier", ""),
                        "parent_hash": rec.get("parent_hash", "")})
                    provenance.record_source(meta_dir, {
                        "url": rec["url"], "source": source, "content_hash": ch,
                        "quality_tier": rec.get("quality_tier", "")})
                if not counted_ns:
                    by_namespace[ns] = by_namespace.get(ns, 0) + 1
                    counted_ns = True
    return {"written": written, "unchanged": unchanged, "by_namespace": by_namespace,
            "by_tier": by_tier, "no_target": no_target}


def main() -> int:
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from-crawl", required=True, help="path to a crawl artifact .json")
    ap.add_argument("--source", required=True, help="source id, e.g. appledocs-docc")
    ap.add_argument("--scraped-at", default="")
    ap.add_argument("--base", default=os.environ.get("DATA_DIR", os.path.join(repo, "data")))
    ap.add_argument("--tier-high-max-failures", type=int, default=None,
                    help="quality failures allowed for the 'high' tier (default 0)")
    ap.add_argument("--tier-medium-max-failures", type=int, default=None,
                    help="quality failures allowed for the 'medium' tier (default 2)")
    ap.add_argument("--keep-low-tier-in-training", action="store_true",
                    help="do NOT restrict low-tier pages to RAG-only (admit to SFT/DAPT)")
    args = ap.parse_args()
    tier_overrides = {"high_max_failures": args.tier_high_max_failures,
                      "medium_max_failures": args.tier_medium_max_failures}
    route_kwargs = {"low_tier_rag_only": not args.keep_low_tier_in_training}
    records = records_from_crawl(args.from_crawl, args.source, args.scraped_at, tier_overrides)
    stats = write_records(records, args.base, args.source, route_kwargs=route_kwargs)
    print(f"build_corpus: {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
