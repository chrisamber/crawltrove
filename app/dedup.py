"""Content deduplication: exact (sha256) + fuzzy (MinHash LSH).

The standard corpus recipe: exact-hash dedup first (cheap), then MinHash LSH
for near-duplicates — FineWeb uses word 5-gram shingles; we use datasketch
with num_perm=128 and Jaccard threshold 0.7 (text-dedup defaults).

Nothing is dropped — duplicates are *flagged* in metadata (duplicate_of /
near_duplicate_of) so the corpus pipeline downstream decides. The index
persists under DATA_DIR/index so it survives restarts. Single-process only,
which matches how this app deploys.
"""
import hashlib
import json
import os
import pickle
import re
import threading
from typing import Any, Dict, Optional

from datasketch import MinHash, MinHashLSH

from app.storage import DATA_DIR

NUM_PERM = 128
JACCARD_THRESHOLD = 0.7
SHINGLE_WORDS = 5

INDEX_DIR = os.path.join(DATA_DIR, "index")
EXACT_PATH = os.path.join(INDEX_DIR, "exact_hashes.json")
LSH_PATH = os.path.join(INDEX_DIR, "minhash_lsh.pkl")

_lock = threading.Lock()
_exact: Optional[Dict[str, str]] = None  # content sha256 -> first key seen
_lsh: Optional[MinHashLSH] = None


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _minhash(text: str) -> MinHash:
    words = _normalize(text).split()
    m = MinHash(num_perm=NUM_PERM)
    if len(words) < SHINGLE_WORDS:
        shingles = [" ".join(words)] if words else []
    else:
        shingles = (
            " ".join(words[i: i + SHINGLE_WORDS])
            for i in range(len(words) - SHINGLE_WORDS + 1)
        )
    for s in shingles:
        m.update(s.encode("utf-8"))
    return m


def _load() -> None:
    global _exact, _lsh
    if _exact is not None:
        return
    os.makedirs(INDEX_DIR, exist_ok=True)
    try:
        with open(EXACT_PATH, encoding="utf-8") as f:
            _exact = json.load(f)
    except Exception:
        _exact = {}
    try:
        with open(LSH_PATH, "rb") as f:
            _lsh = pickle.load(f)
    except Exception:
        _lsh = MinHashLSH(threshold=JACCARD_THRESHOLD, num_perm=NUM_PERM)


def _save() -> None:
    with open(EXACT_PATH, "w", encoding="utf-8") as f:
        json.dump(_exact, f)
    with open(LSH_PATH, "wb") as f:
        pickle.dump(_lsh, f)


def check_and_register(text: str, key: str) -> Dict[str, Any]:
    """Check text against the index, then add it. Returns the dedup report.

    key is typically the page URL; re-registering the same key replaces its
    old signature, and a page is never reported as a duplicate of itself.
    """
    if not text:
        return {"content_hash": "", "exact_duplicate_of": None, "near_duplicate_of": None}

    with _lock:
        _load()
        content_hash = hashlib.sha256(_normalize(text).encode("utf-8")).hexdigest()

        prior = _exact.get(content_hash)
        exact_dup = prior if prior and prior != key else None

        m = _minhash(text)
        near_dup = None
        if not exact_dup:
            matches = [k for k in _lsh.query(m) if k != key]
            near_dup = matches[0] if matches else None

        if prior is None:
            _exact[content_hash] = key
        try:
            if key in getattr(_lsh, "keys", {}):
                _lsh.remove(key)
            _lsh.insert(key, m)
        except ValueError:
            pass
        _save()

    return {
        "content_hash": content_hash,
        "exact_duplicate_of": exact_dup,
        "near_duplicate_of": near_dup,
    }
