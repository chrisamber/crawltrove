#!/usr/bin/env python3
"""Rebuild the whole corpus under the current routing/chunking/tiering rules
(Epic 3 S6).

Re-runs `build_corpus` over every crawl artifact in ``data/crawls/`` into a
fresh generation staged at ``data/corpus.new/``, then **swaps it in on success**
(the previous tree is kept as ``data/corpus.bak/`` for rollback). This is how the
existing corpus benefits from S2 structure-aware chunking + S3 quality tiering
without a re-scrape.

Ledgers are append-only: the rebuild regenerates the corpus tree from scratch
but does NOT rewrite ``metadata/content-hashes.jsonl`` etc. — it appends one
``rebuilds.jsonl`` event describing what was rebuilt. The ``generate_sft`` state
file is carried over unless ``--clear-sft-state`` is given. With
``--reindex-embeddings`` (and an embedding backend configured), the semantic
index is rebuilt afterwards.

Each artifact's corpus source id is taken from its own ``source`` field; override
with ``--source`` (force one) or ``--default-source`` (for artifacts missing it).

Usage:
    python scripts/rebuild_corpus.py
    python scripts/rebuild_corpus.py --source appledocs-docc
    python scripts/rebuild_corpus.py --clear-sft-state --reindex-embeddings
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scripts.build_corpus as build_corpus  # noqa: E402
from app.corpus import provenance  # noqa: E402

SFT_STATE_NAME = ".sft_state.json"


def discover_artifacts(crawls_dir: Path) -> List[Path]:
    """All crawl artifact .json files directly under crawls_dir (checkpoints and
    other subdirectories are skipped)."""
    if not crawls_dir.is_dir():
        return []
    return sorted(p for p in crawls_dir.glob("*.json") if p.is_file())


def _source_for(job: Dict[str, Any], override: Optional[str], default: str) -> str:
    return override or job.get("source") or default


def _merge_counts(dst: Dict[str, int], src: Dict[str, int]) -> None:
    for k, v in (src or {}).items():
        dst[k] = dst.get(k, 0) + v


def rebuild(base, *, source_override: Optional[str] = None,
            default_source: str = "unknown",
            tier_overrides: Dict[str, Any] = None,
            route_kwargs: Dict[str, Any] = None,
            clear_sft_state: bool = False,
            now: str = "") -> Dict[str, Any]:
    """Rebuild data/corpus from data/crawls. Returns a summary dict. Raises if no
    artifacts are found (nothing to rebuild)."""
    base = Path(base)
    crawls = base / "crawls"
    new_corpus = base / "corpus.new"
    old_corpus = base / "corpus"
    bak = base / "corpus.bak"

    artifacts = discover_artifacts(crawls)
    if not artifacts:
        raise SystemExit(f"no crawl artifacts found under {crawls} — nothing to rebuild")

    if new_corpus.exists():
        shutil.rmtree(new_corpus)
    new_corpus.mkdir(parents=True, exist_ok=True)

    totals: Dict[str, int] = {}
    by_tier: Dict[str, int] = {}
    by_source: Dict[str, int] = {}
    for art in artifacts:
        try:
            job = json.loads(art.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  ! skip unreadable {art.name}: {e}")
            continue
        source = _source_for(job, source_override, default_source)
        scraped_at = job.get("end_time") or job.get("scraped_at") or ""
        records = [build_corpus.build_record(r, source, scraped_at, tier_overrides)
                   for r in job.get("results", []) or []]
        stats = build_corpus.write_records(
            records, base, source, route_kwargs=route_kwargs,
            corpus_dir=new_corpus, skip_known=False, record_provenance=False)
        _merge_counts(totals, stats.get("written", {}))
        _merge_counts(by_tier, stats.get("by_tier", {}))
        by_source[source] = by_source.get(source, 0) + sum(stats.get("written", {}).values())
        print(f"  + {art.name}  source={source}  {stats.get('written', {})}")

    # Carry the SFT state forward (record ids changed, so generate_sft will
    # re-derive; keeping it is cheap and honors "clear only on request").
    if not clear_sft_state and old_corpus.exists():
        old_state = old_corpus / SFT_STATE_NAME
        if old_state.exists():
            shutil.copy2(old_state, new_corpus / SFT_STATE_NAME)

    # Atomic-ish swap: old -> bak, new -> corpus.
    if old_corpus.exists():
        if bak.exists():
            shutil.rmtree(bak)
        old_corpus.rename(bak)
    new_corpus.rename(old_corpus)

    meta_dir = base / "metadata"
    meta_dir.mkdir(parents=True, exist_ok=True)
    event = {
        "event": "rebuild",
        "at": now,
        "artifacts": len(artifacts),
        "written": totals,
        "by_tier": by_tier,
        "by_source": by_source,
        "cleared_sft_state": clear_sft_state,
    }
    provenance.record_rebuild(meta_dir, event)
    return event


def _reindex_embeddings() -> None:
    from app import embeddings
    if not embeddings.configured():
        print("  (skipping embeddings reindex — EMBEDDINGS_BASE_URL not set)")
        return
    import asyncio

    import scripts.build_embeddings as be
    print("  reindexing semantic index (full --reindex) …")
    asyncio.run(be.run(list(be.KINDS), reindex=True, limit=0))


def main() -> int:
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=os.environ.get("DATA_DIR", os.path.join(repo, "data")))
    ap.add_argument("--source", default=None, help="force one source id for every artifact")
    ap.add_argument("--default-source", default="unknown",
                    help="source id for artifacts missing a 'source' field")
    ap.add_argument("--tier-high-max-failures", type=int, default=None)
    ap.add_argument("--tier-medium-max-failures", type=int, default=None)
    ap.add_argument("--keep-low-tier-in-training", action="store_true",
                    help="admit low-tier pages to SFT/DAPT (default: RAG-only)")
    ap.add_argument("--clear-sft-state", action="store_true",
                    help="drop the generate_sft state file so SFT fully regenerates")
    ap.add_argument("--reindex-embeddings", action="store_true",
                    help="rebuild the semantic index after the swap (needs a backend)")
    args = ap.parse_args()

    tier_overrides = {"high_max_failures": args.tier_high_max_failures,
                      "medium_max_failures": args.tier_medium_max_failures}
    route_kwargs = {"low_tier_rag_only": not args.keep_low_tier_in_training}
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    summary = rebuild(args.base, source_override=args.source,
                      default_source=args.default_source,
                      tier_overrides=tier_overrides, route_kwargs=route_kwargs,
                      clear_sft_state=args.clear_sft_state, now=now)
    print(f"rebuild_corpus: {json.dumps(summary)}")
    if args.reindex_embeddings:
        _reindex_embeddings()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
