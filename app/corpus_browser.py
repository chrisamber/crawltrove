"""Service-side browser over the offline corpus.

Reads ``data/corpus/**/*.jsonl`` **generically** — it must NOT import
``app.corpus`` (the one-way dependency: the service never depends on the corpus
pipeline). Records are plain dicts with the fields the pipeline writes
(``namespace``, ``framework``, ``license_bucket``, ``quality_tier``, ``text`` …);
the corpus *target* (rag/sft/dapt) comes from the file's path.

The list endpoint streams files and stops once it has a page, so a large JSONL
tree is never fully loaded. ``stats()`` scans once and caches by the tree's file
signature (paths + mtimes), so unchanged trees reuse the cached result.
"""
import json
import os
import threading
from typing import Any, Dict, List, Optional

from app.storage import DATA_DIR

CORPUS_DIR = os.path.join(DATA_DIR, "corpus")
TARGETS = ("rag", "sft", "dapt")
SNIPPET_CHARS = 320

_stats_lock = threading.Lock()
_stats_cache: Dict[str, Any] = {"sig": None, "value": None}


def _jsonl_files() -> List[str]:
    out: List[str] = []
    for target in TARGETS:
        root = os.path.join(CORPUS_DIR, target)
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, names in os.walk(root):
            for n in names:
                if n.endswith(".jsonl"):
                    out.append(os.path.join(dirpath, n))
    return sorted(out)


def _target_of(path: str) -> str:
    rel = os.path.relpath(path, CORPUS_DIR)
    parts = rel.split(os.sep)
    return parts[0] if parts else ""


def _matches(rec: Dict[str, Any], *, namespace, framework, bucket, tier, q) -> bool:
    if namespace and rec.get("namespace") != namespace:
        return False
    if framework and (rec.get("framework") or "") != framework:
        return False
    if bucket and rec.get("license_bucket") != bucket:
        return False
    if tier and (rec.get("quality_tier") or "") != tier:
        return False
    if q:
        ql = q.lower()
        hay = " ".join(str(rec.get(k) or "") for k in ("title", "text", "url", "symbol"))
        if ql not in hay.lower():
            return False
    return True


def _preview(rec: Dict[str, Any], target: str, file_rel: str) -> Dict[str, Any]:
    text = rec.get("text") or ""
    return {
        "id": rec.get("id"),
        "url": rec.get("url"),
        "title": rec.get("title"),
        "namespace": rec.get("namespace"),
        "framework": rec.get("framework"),
        "licenseBucket": rec.get("license_bucket"),
        "qualityTier": rec.get("quality_tier") or "",
        "chunkIndex": rec.get("chunk_index"),
        "headingPath": rec.get("heading_path") or [],
        "target": target,
        "file": f"data/corpus/{file_rel}",
        "snippet": text[:SNIPPET_CHARS],
    }


def browse(*, namespace: Optional[str] = None, framework: Optional[str] = None,
           bucket: Optional[str] = None, tier: Optional[str] = None,
           q: Optional[str] = None, target: Optional[str] = None,
           offset: int = 0, limit: int = 50) -> Dict[str, Any]:
    """Return a filtered, paginated slice of corpus records (previews only).

    Streams files in a stable order and stops once ``limit`` matches are
    collected plus one lookahead (for ``hasMore``) — never loads the whole tree.
    """
    items: List[Dict[str, Any]] = []
    skipped = 0
    has_more = False
    for path in _jsonl_files():
        t = _target_of(path)
        if target and t != target:
            continue
        file_rel = os.path.relpath(path, CORPUS_DIR)
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if not _matches(rec, namespace=namespace, framework=framework,
                                    bucket=bucket, tier=tier, q=q):
                        continue
                    if skipped < offset:
                        skipped += 1
                        continue
                    if len(items) >= limit:
                        has_more = True
                        break
                    items.append(_preview(rec, t, file_rel))
        except OSError:
            continue
        if has_more:
            break
    return {"items": items, "offset": offset, "limit": limit,
            "count": len(items), "hasMore": has_more}


def _signature(files: List[str]) -> str:
    parts = []
    for p in files:
        try:
            parts.append(f"{p}:{os.path.getmtime(p)}")
        except OSError:
            continue
    return "|".join(parts)


def stats() -> Dict[str, Any]:
    """Record counts by target / namespace / bucket / tier, plus distinct filter
    values. Cached by the corpus tree's file signature (recomputed only when a
    JSONL file is added/changed)."""
    files = _jsonl_files()
    sig = _signature(files)
    with _stats_lock:
        if _stats_cache["sig"] == sig and _stats_cache["value"] is not None:
            return _stats_cache["value"]

    total = 0
    by_target: Dict[str, int] = {}
    by_namespace: Dict[str, int] = {}
    by_bucket: Dict[str, int] = {}
    by_tier: Dict[str, int] = {}
    frameworks: set = set()
    for path in files:
        t = _target_of(path)
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    total += 1
                    by_target[t] = by_target.get(t, 0) + 1
                    ns = rec.get("namespace") or "unknown"
                    by_namespace[ns] = by_namespace.get(ns, 0) + 1
                    b = rec.get("license_bucket") or "unknown"
                    by_bucket[b] = by_bucket.get(b, 0) + 1
                    tier = rec.get("quality_tier") or "untiered"
                    by_tier[tier] = by_tier.get(tier, 0) + 1
                    if rec.get("framework"):
                        frameworks.add(rec["framework"])
        except OSError:
            continue

    value = {
        "total": total,
        "byTarget": by_target,
        "byNamespace": by_namespace,
        "byBucket": by_bucket,
        "byTier": by_tier,
        "namespaces": sorted(by_namespace),
        "buckets": sorted(by_bucket),
        "tiers": sorted(by_tier),
        "frameworks": sorted(frameworks),
        "targets": sorted(by_target),
    }
    with _stats_lock:
        _stats_cache["sig"] = sig
        _stats_cache["value"] = value
    return value
