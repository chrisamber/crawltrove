"""Best-effort semantic + keyword retrieval with deterministic RRF fusion."""
from typing import Any, Dict, List, Optional, Tuple

from app import embeddings, vecindex
from app.db import pool, repo


MODES = ("hybrid", "semantic", "keyword")
FACET_FIELDS = ("kind", "namespace", "bucket", "tier", "framework")


class RetrievalUnavailable(RuntimeError):
    pass


def _key(hit: Dict[str, Any]) -> Tuple[str, str, int]:
    return (str(hit.get("kind") or ""), str(hit.get("ref") or ""),
            int(hit.get("chunkIndex") or 0))


def _parent_id(hit: Dict[str, Any]) -> str:
    kind = str(hit.get("kind") or "")
    parent_hash = str((hit.get("meta") or {}).get("parent_hash") or "")
    if parent_hash:
        return f"{kind}:hash:{parent_hash}"
    return f"{kind}:ref:{str(hit.get('ref') or '')}"


def collapse_results(hits: List[Dict[str, Any]], k: int) -> List[Dict[str, Any]]:
    """Keep the best-ranked chunk per stable parent without rewarding length."""
    winners: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for source in hits:
        parent_id = _parent_id(source)
        winner = winners.get(parent_id)
        if winner is None:
            winner = dict(source)
            winner["parentId"] = parent_id
            winner["matchedChunkCount"] = 1
            winners[parent_id] = winner
            order.append(parent_id)
        else:
            winner["matchedChunkCount"] += 1
    return [winners[parent_id] for parent_id in order[:k]]


def facet_counts(hits: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    """Count facet values over already-collapsed retrieval candidates."""
    facets: Dict[str, Dict[str, int]] = {name: {} for name in FACET_FIELDS}
    meta_names = {
        "namespace": "namespace", "bucket": "license_bucket",
        "tier": "quality_tier", "framework": "framework",
    }
    for hit in hits:
        kind = str(hit.get("kind") or "")
        if kind:
            facets["kind"][kind] = facets["kind"].get(kind, 0) + 1
        if kind != "corpus":
            continue
        meta = hit.get("meta") or {}
        for name, key in meta_names.items():
            value = str(meta.get(key) or "")
            if name == "tier" and not value:
                value = "untiered"
            if value:
                facets[name][value] = facets[name].get(value, 0) + 1
    return facets


def reciprocal_rank_fusion(
    semantic: List[Dict[str, Any]],
    keyword: List[Dict[str, Any]],
    *,
    k: int = 10,
    rrf_k: int = 60,
    semantic_weight: float = 1.0,
    keyword_weight: float = 1.0,
) -> List[Dict[str, Any]]:
    """Fuse ranked lists, de-duplicating by stable chunk identity."""
    fused: Dict[Tuple[str, str, int], Dict[str, Any]] = {}
    for signal, hits, weight in (
        ("semantic", semantic, semantic_weight),
        ("keyword", keyword, keyword_weight),
    ):
        seen = set()
        for rank, source in enumerate(hits, 1):
            key = _key(source)
            if key in seen:
                continue
            seen.add(key)
            hit = fused.setdefault(key, dict(source))
            raw_score = source.get("score")
            hit[f"{signal}Rank"] = rank
            hit[f"{signal}Score"] = raw_score
            hit["fusedScore"] = hit.get("fusedScore", 0.0) + weight / (rrf_k + rank)
    out = list(fused.values())
    for hit in out:
        hit["score"] = hit["fusedScore"]
    out.sort(key=lambda hit: (-hit["fusedScore"], _key(hit)))
    return out[:k]


def _ranked(hits: List[Dict[str, Any]], signal: str, k: int) -> List[Dict[str, Any]]:
    out = []
    seen = set()
    for rank, source in enumerate(hits, 1):
        key = _key(source)
        if key in seen:
            continue
        seen.add(key)
        hit = dict(source)
        hit[f"{signal}Rank"] = rank
        hit[f"{signal}Score"] = source.get("score")
        out.append(hit)
        if len(out) >= k:
            break
    return out


def _merge_keyword_sources(
    db_hits: List[Dict[str, Any]], file_hits: List[Dict[str, Any]], depth: int
) -> List[Dict[str, Any]]:
    """Interleave providers so optional Postgres cannot hide file results."""
    out = []
    seen = set()
    # ponytail: two-source round robin; replace with evaluated keyword-source
    # fusion only if the retrieval harness proves ranking quality needs it.
    sources = (db_hits, file_hits)
    positions = [0, 0]
    while len(out) < depth:
        added = False
        for source_index, hits in enumerate(sources):
            while positions[source_index] < len(hits):
                hit = hits[positions[source_index]]
                positions[source_index] += 1
                key = _key(hit)
                if key in seen:
                    continue
                seen.add(key)
                out.append(hit)
                added = True
                break
            if len(out) >= depth:
                break
        if not added:
            break
    return out


def _scrape_ref(row: Dict[str, Any]) -> Optional[str]:
    path = str(row.get("raw_json_path") or "")
    prefix = "data/scrapes/"
    if path.startswith(prefix) and path.endswith(".json"):
        return path[len(prefix):-len(".json")]
    return None


def _bridge_db_rows(rows: List[Dict[str, Any]], semantic: List[Dict[str, Any]],
                    depth: int) -> List[Dict[str, Any]]:
    refs = [ref for row in rows if (ref := _scrape_ref(row))]
    indexed = vecindex.chunks_for_refs(
        refs, kind="scrape", k=max(depth * 4, depth))
    by_ref: Dict[str, List[Dict[str, Any]]] = {}
    # Prefer current semantic candidates, so a page-level DB hit overlaps the
    # exact chunk already competing in the semantic list.
    for hit in indexed:
        by_ref.setdefault(str(hit.get("ref") or ""), []).append(hit)
    for hit in reversed(semantic):
        if hit.get("kind") != "scrape":
            continue
        by_ref.setdefault(str(hit.get("ref") or ""), []).insert(0, hit)

    out = []
    used = set()
    for row in rows:
        url = str(row.get("url") or "")
        ref = _scrape_ref(row)
        match = next(
            (h for h in by_ref.get(ref or "", []) if _key(h) not in used), None)
        if match is not None:
            hit = dict(match)
        else:
            hit = {
                "kind": "scrape", "ref": f"db:{row.get('id')}", "url": url or None,
                "chunkIndex": 0, "meta": row.get("metadata") or {},
            }
        hit["snippet"] = row.get("snippet") or hit.get("snippet") or ""
        hit["score"] = float(row.get("rank") or 0.0)
        used.add(_key(hit))
        out.append(hit)
        if len(out) >= depth:
            break
    return out


async def _keyword(query: str, kind: Optional[str], depth: int,
                   filters: Optional[Dict[str, str]],
                   semantic: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], bool]:
    # Postgres only indexes scraped pages; other kinds go directly to files.
    db_hits: List[Dict[str, Any]] = []
    if not filters and pool.enabled() and kind in (None, "scrape"):
        try:
            rows = await repo.search_pages(query, limit=depth)
        except Exception:
            rows = []
        if rows:
            db_hits = _bridge_db_rows(rows, semantic, depth)
    file_available = vecindex.available()
    if file_available:
        kwargs: Dict[str, Any] = {"kind": kind, "k": depth}
        if filters:
            kwargs["filters"] = filters
        file_hits = vecindex.keyword_search(query, **kwargs)
        return _merge_keyword_sources(db_hits, file_hits, depth), True
    if db_hits:
        return db_hits[:depth], True
    return [], bool(not filters and pool.enabled() and kind in (None, "scrape"))


async def search(query: str, *, kind: Optional[str] = None, k: int = 10,
                 mode: str = "hybrid", candidate_depth: Optional[int] = None,
                 rrf_k: int = 60,
                 filters: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """Run the selected signal(s); backend failures degrade to the other one."""
    max_depth = max(k, min(200, candidate_depth or 200))
    depth = min(max_depth, max(50, k * 5))
    semantic_available = embeddings.configured() and vecindex.available()
    semantic_vector: Optional[List[float]] = None
    if mode in ("hybrid", "semantic") and semantic_available:
        try:
            semantic_vector = await embeddings.embed_query(query)
        except Exception:
            semantic_vector = None

    while True:
        semantic_hits: List[Dict[str, Any]] = []
        if semantic_vector is not None:
            try:
                kwargs: Dict[str, Any] = {"kind": kind, "k": depth}
                if filters:
                    kwargs["filters"] = filters
                semantic_hits = vecindex.search(semantic_vector, **kwargs)
            except Exception:
                semantic_hits = []

        keyword_hits: List[Dict[str, Any]] = []
        keyword_available = False
        if mode in ("hybrid", "keyword"):
            try:
                keyword_hits, keyword_available = await _keyword(
                    query, kind, depth, filters, semantic_hits)
            except Exception:
                keyword_hits = []
                keyword_available = vecindex.available()

        if mode == "semantic":
            if not semantic_available:
                raise RetrievalUnavailable(
                    "semantic search is not configured or available")
            result = collapse_results(
                _ranked(semantic_hits, "semantic", depth), k)
        elif mode == "keyword":
            if not keyword_available:
                raise RetrievalUnavailable(
                    "keyword search is not configured or available")
            result = collapse_results(_ranked(keyword_hits, "keyword", depth), k)
        else:
            if not semantic_available and not keyword_available:
                raise RetrievalUnavailable(
                    "no retrieval signal is configured or available")
            fused = reciprocal_rank_fusion(
                semantic_hits, keyword_hits,
                k=len(semantic_hits) + len(keyword_hits), rrf_k=rrf_k)
            result = collapse_results(fused, k)
        if len(result) >= k or depth >= max_depth:
            return result
        depth = min(max_depth, depth * 2)


async def facets(query: str, *, kind: Optional[str] = None,
                 mode: str = "hybrid", limit: int = 200) -> Dict[str, Any]:
    """Query-scoped facet counts over unique parent candidates."""
    hits = await search(query, kind=kind, k=limit, mode=mode,
                        candidate_depth=limit)
    return {
        "facets": facet_counts(hits),
        "candidateCount": len(hits),
        "candidateLimit": limit,
        "truncated": len(hits) >= limit,
    }
