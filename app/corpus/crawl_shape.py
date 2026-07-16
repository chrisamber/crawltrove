"""Normalize heterogeneous scraper outputs into the single crawl-artifact shape
that scripts/build_corpus.py --from-crawl consumes.

Only appledocs-docc writes a native crawl artifact (via storage.save_crawl);
swift-evolution / wwdc / sample-code / web all produce flat records, so the
driver runs them through here first.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional


def result_from_flat(rec: Dict[str, Any], source: str) -> Optional[Dict[str, Any]]:
    meta_in = rec.get("meta") or {}
    url = rec.get("url") or meta_in.get("url") or ""
    text = rec.get("text") or rec.get("markdown") or ""
    if not url or not text:
        return None
    metadata: Dict[str, Any] = {k: v for k, v in rec.items()
                                if k not in ("text", "markdown", "url", "meta")}
    metadata.update(meta_in)
    metadata["source"] = source
    return {"url": url, "markdown": text, "metadata": metadata}


def job_from_results(results: List[Dict[str, Any]], *, base_url: str,
                     source: str, now: str) -> Dict[str, Any]:
    return {
        "jobId": None,
        "base_url": base_url,
        "status": "completed",
        "engine": "normalized",
        "source": source,
        "total": len(results),
        "skipped": [],
        "start_time": now,
        "end_time": now,
        "results": results,
    }


def read_flat_jsonl(path, source: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        res = result_from_flat(json.loads(line), source)
        if res is not None:
            out.append(res)
    return out
