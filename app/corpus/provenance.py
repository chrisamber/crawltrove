"""Append-only provenance side-files under metadata/.

Every retrieved RAG answer must be able to cite its source; these files are the
ledger that makes diff-based refresh and citation possible.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict


def append_jsonl(path, obj: Dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def load_content_hashes(meta_dir) -> Dict[str, Dict]:
    path = Path(meta_dir) / "content-hashes.jsonl"
    out: Dict[str, Dict] = {}
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            out[rec["content_hash"]] = rec
    return out


def record_content_hash(meta_dir, content_hash: str, entry: Dict) -> bool:
    existing = load_content_hashes(meta_dir)
    if content_hash in existing:
        return False
    row = dict(entry)
    row["content_hash"] = content_hash
    append_jsonl(Path(meta_dir) / "content-hashes.jsonl", row)
    return True


def record_source(meta_dir, entry: Dict) -> None:
    append_jsonl(Path(meta_dir) / "sources.jsonl", entry)


def record_license(meta_dir, entry: Dict) -> None:
    append_jsonl(Path(meta_dir) / "licenses.jsonl", entry)


def record_scrape_run(meta_dir, entry: Dict) -> None:
    append_jsonl(Path(meta_dir) / "scrape-runs.jsonl", entry)


def record_rebuild(meta_dir, entry: Dict) -> None:
    """Append a corpus-rebuild event. The rebuild regenerates the
    corpus tree from scratch but must NOT rewrite ledger history — it appends
    one of these events instead (what was rebuilt, from how many artifacts)."""
    append_jsonl(Path(meta_dir) / "rebuilds.jsonl", entry)
