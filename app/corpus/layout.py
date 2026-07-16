"""Canonical on-disk layout for the split corpus.

corpus/ and metadata/ live under DATA_DIR (git-ignored) because they are large
generated artifacts, not source. Mirrors the spec's corpus/ + metadata/ tree.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict

RAG = "rag"
SFT = "sft"
DAPT = "dapt"


def ensure_layout(base) -> Dict[str, Path]:
    base = Path(base)
    corpus = base / "corpus"
    metadata = base / "metadata"
    paths = {
        "corpus": corpus,
        "rag": corpus / RAG,
        "sft": corpus / SFT,
        "dapt": corpus / DAPT,
        "eval": corpus / "eval",
        "metadata": metadata,
        "sources": metadata / "sources.jsonl",
        "licenses": metadata / "licenses.jsonl",
        "scrape_runs": metadata / "scrape-runs.jsonl",
        "content_hashes": metadata / "content-hashes.jsonl",
    }
    for key in ("rag", "sft", "dapt", "eval", "metadata"):
        paths[key].mkdir(parents=True, exist_ok=True)
    return paths
