"""Load + validate the Swift 6.4 / iOS 27 scrape manifest.

A batch is one unit of work for the drain loop. `source` picks the scraper;
`corpus_source` (optional) overrides the id passed to build_corpus --source
(used by `web` batches that are really TSPL / swift.org content).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import yaml

KNOWN_SOURCES = {"appledocs-docc", "swift-evolution", "wwdc", "web"}


def load_manifest(path) -> Dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def batches(m: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(m.get("batches") or [])


def corpus_source(batch: Dict[str, Any]) -> str:
    return batch.get("corpus_source") or batch.get("source", "")


def validate_manifest(m: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    bs = m.get("batches")
    if not isinstance(bs, list):
        return ["manifest 'batches' must be a list"]
    seen_ids = set()
    for i, b in enumerate(bs):
        where = b.get("id", f"#{i}")
        if not b.get("id"):
            errors.append(f"batch {where}: missing id")
        elif b["id"] in seen_ids:
            errors.append(f"batch {where}: duplicate id")
        else:
            seen_ids.add(b["id"])
        src = b.get("source")
        if src not in KNOWN_SOURCES:
            errors.append(f"batch {where}: bad source {src!r} (known: {sorted(KNOWN_SOURCES)})")
        if not b.get("version_hint"):
            errors.append(f"batch {where}: missing version_hint")
        if src == "appledocs-docc" and not b.get("root"):
            errors.append(f"batch {where}: appledocs-docc needs a root URL")
        if src == "web" and not b.get("urls"):
            errors.append(f"batch {where}: web batch needs a urls list")
    return errors
