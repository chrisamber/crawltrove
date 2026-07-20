"""Route a record to RAG / SFT / DAPT datasets by license bucket.

Policy (from the spec's "practical rule"):
  RAG  : Apple docs + Swift.org + sample code, with citations.
  SFT  : own task/answer pairs only (NEVER auto from scraped docs).
  DAPT : permissively-licensed code/docs + own corpus; avoid bulk Apple docs.

Quality-tiered routing keeps a ``low`` quality tier out of
SFT/DAPT by default (RAG keeps everything, tier stamped on the record). This is
consumer-side filtering — the service still reports quality untouched.
"""
from __future__ import annotations

import os
from typing import Dict, Set

POLICY: Dict[str, Set[str]] = {
    "apple-developer-docs-review-required": {"rag"},
    "apple-sample-code-review-required": {"rag"},
    "swift-org-permissive": {"rag", "dapt"},
    "cc-by-4.0": {"rag", "dapt"},
    "cc0-1.0": {"rag", "dapt"},
    # Permissive code/docs are DAPT-eligible; community-contributed
    # content (forums, unconfirmed tutorials/repos) is RAG-only until reviewed.
    "permissive": {"rag", "dapt"},
    "community-review-required": {"rag"},
    "own-content": {"rag", "sft", "dapt"},
    "unknown": set(),
}

# Repo licenses permissive enough to admit sample code to DAPT.
_PERMISSIVE_REPO = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "CC0-1.0"}


def _low_tier_rag_only_default() -> bool:
    return os.environ.get("CORPUS_LOW_TIER_RAG_ONLY", "true").lower() in ("1", "true", "yes")


def route(record: Dict, *, low_tier_rag_only: bool = None) -> Set[str]:
    bucket = record.get("license_bucket", "unknown")
    targets = set(POLICY.get(bucket, set()))
    if bucket == "apple-sample-code-review-required":
        if record.get("repo_license") in _PERMISSIVE_REPO:
            targets = targets | {"dapt"}
    if low_tier_rag_only is None:
        low_tier_rag_only = _low_tier_rag_only_default()
    # Low-tier pages are RAG-only: drop SFT/DAPT so noisy content never enters
    # training packs. RAG keeps everything (the tier is stamped for auditing).
    if low_tier_rag_only and record.get("quality_tier") == "low":
        targets = targets & {"rag"}
    return targets
