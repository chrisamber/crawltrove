"""Map a page's quality report to a corpus quality tier (Epic 3 S3).

The service's quality signal (app/quality.py) *scores, never drops* — it returns
``{passed, failures, signals}``. The corpus pipeline is the consumer and *is*
allowed to filter, so here we fold that report into a coarse tier
(``high|medium|low``) that the router uses to keep low-quality pages out of
SFT/DAPT while still admitting them to RAG.

Thresholds live in one config dict and are overridable via env (or the
build_corpus CLI, which passes overrides through). A page with no quality report
is treated as ``medium`` — neutral, never over-penalized.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

# A page is HIGH with at most this many failed heuristics, MEDIUM with at most
# the medium bound, else LOW. (quality.assess yields a list of failed rules.)
DEFAULT_THRESHOLDS: Dict[str, int] = {
    "high_max_failures": 0,
    "medium_max_failures": 2,
}

ALLOWED_TIERS = ("high", "medium", "low")


def resolve_thresholds(high_max_failures: Optional[int] = None,
                       medium_max_failures: Optional[int] = None) -> Dict[str, int]:
    """Explicit args win, then env (CORPUS_TIER_HIGH_MAX_FAILURES /
    CORPUS_TIER_MEDIUM_MAX_FAILURES), then DEFAULT_THRESHOLDS."""
    def pick(arg, env_key, default):
        if arg is not None:
            return arg
        v = os.environ.get(env_key)
        if v is not None:
            try:
                return int(v)
            except ValueError:
                pass
        return default

    return {
        "high_max_failures": pick(high_max_failures, "CORPUS_TIER_HIGH_MAX_FAILURES",
                                  DEFAULT_THRESHOLDS["high_max_failures"]),
        "medium_max_failures": pick(medium_max_failures, "CORPUS_TIER_MEDIUM_MAX_FAILURES",
                                    DEFAULT_THRESHOLDS["medium_max_failures"]),
    }


def tier_for(quality_meta: Optional[Dict[str, Any]], *,
             high_max_failures: Optional[int] = None,
             medium_max_failures: Optional[int] = None) -> str:
    """Return 'high' | 'medium' | 'low' for a quality report.

    Uses the failed-heuristic count from quality.assess. Falls back to the
    ``passed`` boolean if no ``failures`` list is present, and to 'medium' when
    there is no report at all."""
    if not quality_meta:
        return "medium"
    th = resolve_thresholds(high_max_failures, medium_max_failures)
    failures = quality_meta.get("failures")
    if failures is None:
        passed = quality_meta.get("passed")
        if passed is True:
            return "high"
        if passed is False:
            return "low"
        return "medium"
    n = len(failures)
    if n <= th["high_max_failures"]:
        return "high"
    if n <= th["medium_max_failures"]:
        return "medium"
    return "low"
