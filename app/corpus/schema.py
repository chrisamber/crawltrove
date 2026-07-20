"""Canonical corpus record: the minimum metadata every chunk carries."""
from __future__ import annotations

from typing import Any, Dict, List

REQUIRED_FIELDS: List[str] = [
    "id", "source", "url", "title", "framework", "symbol", "symbol_kind",
    "platforms", "availability", "swift_version", "xcode_version",
    "scraped_at", "license_bucket", "content_hash", "chunk_type",
    "namespace", "text",
]

ALLOWED_CHUNK_TYPES = {
    "symbol_card", "symbol_discussion", "tutorial_step", "release_note_item",
    "migration_note", "diagnostic_explanation", "sample_code_file",
    "wwdc_transcript_segment", "evolution_proposal_section",
}

ALLOWED_NAMESPACES = {
    "swift-language", "swift-stdlib", "apple-framework", "xcode-tooling",
    "unknown",
}

# Quality tiers are optional; "" means untiered (legacy records).
ALLOWED_QUALITY_TIERS = {"high", "medium", "low", ""}

# Loosened (2026-06-17): only the source URL is enforced non-empty — a URL-less
# chunk is genuine tech debt the spec forbids ("Do not remove source URLs").
# Other fields are presence-checked only and may be empty during development.
_NON_EMPTY = {"url"}


def new_record(**fields: Any) -> Dict[str, Any]:
    rec: Dict[str, Any] = {
        "id": "",
        "source": "",
        "url": "",
        "title": "",
        "framework": "",
        "symbol": "",
        "symbol_kind": "",
        "platforms": [],
        "availability": {"introduced": None, "deprecated": None, "beta": False},
        "swift_version": "",
        "xcode_version": "",
        "scraped_at": "",
        "license_bucket": "unknown",
        "content_hash": "",
        "chunk_type": "symbol_card",
        "namespace": "unknown",
        "text": "",
        # Structure-aware chunking is optional; a page-level record
        # leaves chunk_index=0, parent_hash="", heading_path=[].
        "chunk_index": 0,
        "parent_hash": "",
        "heading_path": [],
        # Quality-tiered routing is optional; "" == untiered.
        "quality_tier": "",
    }
    rec.update(fields)
    return rec


def validate_record(rec: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    for field in REQUIRED_FIELDS:
        if field not in rec:
            errors.append(f"missing required field: {field}")
            continue
        if field in _NON_EMPTY and not rec[field]:
            errors.append(f"field must not be empty: {field}")
    if rec.get("chunk_type") not in ALLOWED_CHUNK_TYPES:
        errors.append(f"bad chunk_type: {rec.get('chunk_type')!r}")
    if rec.get("namespace") not in ALLOWED_NAMESPACES:
        errors.append(f"bad namespace: {rec.get('namespace')!r}")
    # Optional chunk/tier fields: validate only when present; older records
    # simply omit them.
    if "chunk_index" in rec and not isinstance(rec["chunk_index"], int):
        errors.append(f"chunk_index must be an int: {rec.get('chunk_index')!r}")
    if "heading_path" in rec and not isinstance(rec["heading_path"], list):
        errors.append(f"heading_path must be a list: {rec.get('heading_path')!r}")
    if rec.get("quality_tier", "") not in ALLOWED_QUALITY_TIERS:
        errors.append(f"bad quality_tier: {rec.get('quality_tier')!r}")
    return errors
