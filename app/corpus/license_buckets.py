"""Map a content source (+ optional detected license) to a license bucket.

License is tracked PER SOURCE, not per corpus. A page-level CC marker is not
proof the underlying content is freely licensed, so Apple sources never get
upgraded by a detected marker.
"""
from __future__ import annotations

from typing import Optional

ALL_BUCKETS = {
    "apple-developer-docs-review-required",
    "apple-sample-code-review-required",
    "swift-org-permissive",
    "cc-by-4.0",
    "cc0-1.0",
    # Epic 3 S5 new sources:
    "permissive",                 # MIT/Apache/BSD/ISC code + docs (RAG + DAPT)
    "community-review-required",  # forums / unconfirmed tutorials & repos (RAG only)
    "own-content",
    "unknown",
}

SOURCE_BUCKET = {
    "appledocs-docc": "apple-developer-docs-review-required",
    "wwdc": "apple-developer-docs-review-required",
    "samplecode": "apple-sample-code-review-required",
    "swift-evolution": "swift-org-permissive",
    "swift-book": "cc-by-4.0",
    "own": "own-content",
    # Epic 3 S5 collectors. swift.org is Apache-2.0 (permissive). Forums are
    # user-contributed → conservative RAG-only. Tutorials/GitHub are tagged by
    # the collector with a license-specific source id (see the helpers below);
    # the bare ids are the conservative fallback when the license is unconfirmed.
    "swiftorg": "swift-org-permissive",
    "swift-forums": "community-review-required",
    "tutorials": "community-review-required",
    "tutorials-cc-by": "cc-by-4.0",
    "tutorials-cc0": "cc0-1.0",
    "github-permissive": "permissive",
    "github-cc-by": "cc-by-4.0",
    "github-docs": "community-review-required",
}

# Detected-license markers that may upgrade an otherwise-unknown source.
_CC_UPGRADES = {
    "CC-BY-4.0": "cc-by-4.0",
    "CC-BY-SA-4.0": "cc-by-4.0",
    "CC0-1.0": "cc0-1.0",
}

# SPDX ids permissive enough for RAG + DAPT (code + docs). Used by the GitHub
# collector to pick a source id from a repo's LICENSE.
SPDX_PERMISSIVE = {
    "MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "BSD-2-Clause-Patent",
    "ISC", "0BSD", "Unlicense", "Zlib",
}


def source_id_for_repo_license(spdx: Optional[str]) -> str:
    """GitHub collector: map a repo's SPDX license to a corpus source id. A
    permissive license unlocks DAPT; anything else (copyleft/unknown) stays
    conservative (RAG-only via community-review-required)."""
    if spdx and spdx in SPDX_PERMISSIVE:
        return "github-permissive"
    return "github-docs"


def source_id_for_detected_license(license_id: Optional[str]) -> str:
    """Tutorials collector: map a page-level detected license to a source id.
    Only a confirmed CC license unlocks permissive routing; otherwise the page
    is RAG-only (community-review-required)."""
    if license_id in ("CC-BY-4.0", "CC-BY-SA-4.0"):
        return "tutorials-cc-by"
    if license_id == "CC0-1.0":
        return "tutorials-cc0"
    return "tutorials"


def bucket_for(source: str, license_id: Optional[str] = None) -> str:
    default = SOURCE_BUCKET.get(source, "unknown")
    # Apple sources are pinned regardless of any detected marker.
    if default.startswith("apple-"):
        return default
    if default != "unknown":
        return default
    if license_id and license_id in _CC_UPGRADES:
        return _CC_UPGRADES[license_id]
    return "unknown"
