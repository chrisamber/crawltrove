"""Deterministic parent-level retrieval evaluation over the CrawlTrove API."""
import glob
import hashlib
import json
import math
import os
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

import httpx

from app.normalize import normalize_url


MODES = ("semantic", "keyword", "hybrid")
FILTERS = ("kind", "namespace", "bucket", "tier", "framework")


class EvalError(RuntimeError):
    pass


def load_cases(cases_dir: str) -> List[Dict[str, Any]]:
    cases = []
    for path in sorted(glob.glob(os.path.join(cases_dir, "*.json"))):
        try:
            with open(path, encoding="utf-8") as handle:
                case = json.load(handle)
        except Exception as exc:
            raise EvalError(f"cannot read {path}: {exc}") from exc
        name = case.get("name")
        query = case.get("query")
        relevant = case.get("relevantIds")
        if not isinstance(name, str) or not name.strip():
            raise EvalError(f"{path}: name must be a non-empty string")
        if not isinstance(query, str) or not query.strip():
            raise EvalError(f"{path}: query must be a non-empty string")
        if (not isinstance(relevant, list) or not relevant
                or any(not isinstance(item, str) or not item.strip()
                       for item in relevant)):
            raise EvalError(f"{path}: relevantIds must be a non-empty string list")
        if len(set(relevant)) != len(relevant):
            raise EvalError(f"{path}: relevantIds must be unique")
        filters = case.get("filters") or {}
        if not isinstance(filters, dict) or any(key not in FILTERS for key in filters):
            raise EvalError(f"{path}: filters contain an unsupported field")
        tags = case.get("tags") or []
        if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
            raise EvalError(f"{path}: tags must be a string list")
        cases.append(case)
    if not cases:
        raise EvalError(f"no retrieval cases found in {cases_dir}")
    return cases


def case_set_hash(cases: Sequence[Dict[str, Any]]) -> str:
    payload = json.dumps(cases, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def result_identities(hit: Dict[str, Any]) -> Set[str]:
    kind = str(hit.get("kind") or "")
    identities = set()
    parent_id = hit.get("parentId")
    if parent_id:
        identities.add(str(parent_id))
    ref = hit.get("ref")
    if kind and ref:
        identities.add(f"{kind}:ref:{ref}")
    url = hit.get("url")
    if kind and url:
        identities.add(f"{kind}:url:{normalize_url(str(url))}")
    return identities


def score_case(relevant_ids: Sequence[str], hits: Sequence[Dict[str, Any]],
               k: int) -> Dict[str, Any]:
    relevant = set(relevant_ids)
    matched = set()
    relevant_ranks = []
    seen_results = set()
    for rank, hit in enumerate(hits[:k], 1):
        identities = result_identities(hit)
        primary = str(hit.get("parentId") or
                      f"{hit.get('kind', '')}:ref:{hit.get('ref', '')}")
        if primary in seen_results:
            continue
        seen_results.add(primary)
        matches = sorted((relevant - matched) & identities)
        if matches:
            matched.add(matches[0])
            relevant_ranks.append(rank)
    recall = len(matched) / len(relevant) if relevant else 1.0
    mrr = 1.0 / relevant_ranks[0] if relevant_ranks else 0.0
    dcg = sum(1.0 / math.log2(rank + 1) for rank in relevant_ranks)
    ideal_count = min(len(relevant), k)
    ideal = sum(1.0 / math.log2(rank + 1)
                for rank in range(1, ideal_count + 1))
    return {
        "recall": recall,
        "mrr": mrr,
        "ndcg": dcg / ideal if ideal else 1.0,
        "firstRelevantRank": relevant_ranks[0] if relevant_ranks else None,
        "matchedIds": sorted(matched),
        "missingIds": sorted(relevant - matched),
    }


def aggregate(scores: Sequence[Dict[str, Any]]) -> Dict[str, float]:
    if not scores:
        return {"recall": 0.0, "mrr": 0.0, "ndcg": 0.0}
    return {
        name: sum(float(score[name]) for score in scores) / len(scores)
        for name in ("recall", "mrr", "ndcg")
    }


async def fetch_hits(client: httpx.AsyncClient, case: Dict[str, Any],
                     mode: str, k: int) -> List[Dict[str, Any]]:
    params = {"q": case["query"], "mode": mode, "k": str(k)}
    params.update({key: str(value) for key, value in (case.get("filters") or {}).items()
                   if value is not None and str(value)})
    response = await client.get("/api/search/hybrid", params=params)
    if response.status_code == 501:
        detail = response.json().get("detail", "retrieval mode unavailable")
        raise EvalError(f"{mode} unavailable: {detail}")
    if response.status_code != 200:
        raise EvalError(
            f"{mode} search failed with HTTP {response.status_code}: {response.text}")
    return list(response.json().get("results") or [])


async def evaluate_mode(cases: Sequence[Dict[str, Any]], mode: str, k: int,
                        client: httpx.AsyncClient) -> Dict[str, Any]:
    rows = []
    for case in cases:
        hits = await fetch_hits(client, case, mode, k)
        score = score_case(case["relevantIds"], hits, k)
        rows.append({
            "name": case["name"], "tags": case.get("tags") or [], **score,
        })
    exact = [row for row in rows if "exact-symbol" in row["tags"]]
    return {
        "mode": mode,
        "cases": rows,
        "aggregate": aggregate(rows),
        "exactSymbol": aggregate(exact) if exact else None,
    }


def unresolved_ids(cases: Sequence[Dict[str, Any]],
                   inventory: Iterable[str]) -> List[str]:
    available = set(inventory)
    required = {identity for case in cases for identity in case["relevantIds"]}
    return sorted(required - available)


def unresolved_case_ids(cases: Sequence[Dict[str, Any]],
                        inventory_for_filters) -> List[str]:
    unresolved = []
    for case in cases:
        inventory = inventory_for_filters(case.get("filters") or {})
        for identity in unresolved_ids([case], inventory):
            unresolved.append(f"{case['name']}: {identity}")
    return unresolved


def gate_reports(reports: Dict[str, Dict[str, Any]],
                 tolerance: float = 1e-12) -> List[str]:
    if "semantic" not in reports or "hybrid" not in reports:
        raise EvalError("--gate requires both semantic and hybrid modes")
    reasons = []
    semantic = reports["semantic"]
    hybrid = reports["hybrid"]
    for metric in ("recall", "mrr", "ndcg"):
        if hybrid["aggregate"][metric] + tolerance < semantic["aggregate"][metric]:
            reasons.append(f"hybrid aggregate {metric} regressed")
        sem_exact = semantic.get("exactSymbol")
        hyb_exact = hybrid.get("exactSymbol")
        if sem_exact is not None and hyb_exact is not None:
            if hyb_exact[metric] + tolerance < sem_exact[metric]:
                reasons.append(f"hybrid exact-symbol {metric} regressed")
    return reasons
