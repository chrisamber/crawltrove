#!/usr/bin/env python3
"""Merge the extra dense sources into the clean DAPT split -> mlx-clean-plus.

    mlx-clean-plus  =  mlx-clean  ∪  (unique · in-domain · quality-pass extras)

Design (kept deliberately apples-to-apples for the token-gain A/B):
  * mlx-clean's valid.jsonl and test.jsonl are copied VERBATIM. The held-out
    eval set is therefore identical across mlx-clean and mlx-clean-plus, so a
    val/test-loss delta reflects the added *training* signal, nothing else.
  * Every surviving new record is appended to the TRAIN split only.

Dedup (exact, content-hash): a new record is dropped if its normalized-text
sha256 (the same hash app.dedup writes as meta.content_hash) already exists in:
  - the canonical clean corpus (avfoundation-audio.jsonl meta.content_hash —
    a superset of mlx-clean), OR
  - mlx-clean's own text (recomputed), OR
  - an earlier-accepted new record (intra-extra dedup).
Near-duplicates (meta.near_duplicate_of set by the scrapers) are NOT dropped,
only counted — corpus convention is flag-don't-filter for fuzzy dups.

Keep rule (the py3langid-mislabels-code gotcha):
  - WWDC transcripts (prose): require language == 'en' AND quality_passed.
  - sample-code source: a .swift/.h/.m/.mm/.c/.cpp/.metal/.md file is kept if
    quality_passed, regardless of the language tag (langid routinely calls
    code-only files non-English); a non-code file must also be language=='en'.
    Rationale: the corpus target is Apple *audio* code; dropping idiomatic
    Swift because langid guessed "cy"/"de" would discard the densest signal.

Output: data/dapt/mlx-clean-plus/{train,valid,test}.jsonl  ({"text"} only)
        data/dapt/mlx-clean-plus/stats.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from typing import Any, Dict, List, Set, Tuple

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app import dedup  # _normalize, for the canonical content hash  # noqa: E402

DAPT = os.path.join(_REPO_ROOT, "data", "dapt")
CLEAN_DIR = os.path.join(DAPT, "mlx-clean")
EXTRA_DIR = os.path.join(DAPT, "extra-sources")
OUT_DIR = os.path.join(DAPT, "mlx-clean-plus")
SRC_CORPUS = os.path.join(DAPT, "avfoundation-audio.jsonl")

CODE_EXTS = (".swift", ".h", ".m", ".mm", ".c", ".cpp", ".cc", ".metal")


def _chash(text: str) -> str:
    return hashlib.sha256(dedup._normalize(text).encode("utf-8")).hexdigest()


def _est_tokens(chars: int) -> int:
    return chars // 4


def _read_jsonl(path: str) -> List[Dict[str, Any]]:
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def existing_hashes() -> Tuple[Set[str], Set[str]]:
    """(canonical-corpus hashes, mlx-clean hashes). Both via the same normalizer."""
    corpus: Set[str] = set()
    with open(SRC_CORPUS, encoding="utf-8") as f:
        for line in f:
            try:
                m = json.loads(line)["meta"]
            except Exception:
                continue
            h = m.get("content_hash")
            if h:
                corpus.add(h)
    clean: Set[str] = set()
    for split in ("train", "valid", "test"):
        for r in _read_jsonl(os.path.join(CLEAN_DIR, f"{split}.jsonl")):
            clean.add(_chash(r["text"]))
    return corpus, clean


def _code_substance_ok(text: str) -> bool:
    """A code-appropriate floor that the prose Gopher/FineWeb gate can't express.

    The standard quality gate is built for prose and rejects ~88% of these Swift/
    C files on `alpha_words` alone — it counts `{`, `}`, `->`, `()` as non-alpha
    "words", so idiomatic source code looks like junk to it. That gate is simply
    the wrong instrument for code. Instead we keep a code file unless it is a
    near-trivial stub: it must have real size, enough non-blank lines, and a
    handful of genuinely code-shaped lines (not just a license header).
    """
    lines = [l for l in text.splitlines() if l.strip()]
    if len(text) < 200 or len(lines) < 8:
        return False
    codeish = sum(1 for l in lines if any(c in l for c in "{}()=;:<>[]"))
    return codeish >= 5


def keep_record(rec: Dict[str, Any]) -> bool:
    """Keep rule, differentiated by content type.

    * Prose (WWDC transcripts; sample-code .md): strict — language == 'en' AND
      the prose quality gate passed.
    * Code (.swift/.h/.m/.mm/.c/.cpp/.metal): the prose quality verdict and the
      langid tag are both unreliable for source, so we substitute a code-
      substance floor (see _code_substance_ok) and ignore both.
    """
    meta = rec.get("meta", {})
    src = meta.get("source", "")
    ext = (meta.get("ext") or "").lower()
    is_code = src == "samplecode" and ext in CODE_EXTS
    if is_code:
        return _code_substance_ok(rec.get("text", ""))
    # prose path (transcripts + .md): keep the strict gate
    if not (meta.get("quality_passed") is True):
        return False
    return meta.get("language") == "en"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sources", default="wwdc,samplecode",
                    help="comma list of extra-sources stems to merge")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)

    clean_train = _read_jsonl(os.path.join(CLEAN_DIR, "train.jsonl"))
    clean_valid = _read_jsonl(os.path.join(CLEAN_DIR, "valid.jsonl"))
    clean_test = _read_jsonl(os.path.join(CLEAN_DIR, "test.jsonl"))

    corpus_hashes, clean_hashes = existing_hashes()
    # the full "already present" set new records must not duplicate
    seen: Set[str] = set(corpus_hashes) | set(clean_hashes)
    print(f"existing content hashes: corpus={len(corpus_hashes)} "
          f"clean={len(clean_hashes)} union={len(seen)}", flush=True)

    # Per-source accounting
    src_stats: Dict[str, Dict[str, Any]] = {}
    added_records: List[Dict[str, Any]] = []

    for src in [s.strip() for s in args.sources.split(",") if s.strip()]:
        path = os.path.join(EXTRA_DIR, f"{src}.jsonl")
        recs = _read_jsonl(path)
        st = {
            "input_records": len(recs),
            "kept": 0,
            "added_tokens": 0,
            "dropped_exact_dup": 0,
            "dropped_by_keep_gate": 0,  # prose: !en or !quality; code: stub
            "near_dups_flagged": 0,
            "exact_dup_vs_cleancorpus": 0,
            "exact_dup_vs_intra_extra": 0,
        }
        for rec in recs:
            meta = rec.get("meta", {})
            if meta.get("near_duplicate_of"):
                st["near_dups_flagged"] += 1
            if not keep_record(rec):
                st["dropped_by_keep_gate"] += 1
                continue
            text = rec["text"]
            h = _chash(text)
            if h in seen:
                st["dropped_exact_dup"] += 1
                # attribute the collision
                if h in corpus_hashes or h in clean_hashes:
                    st["exact_dup_vs_cleancorpus"] += 1
                else:
                    st["exact_dup_vs_intra_extra"] += 1
                continue
            seen.add(h)
            added_records.append({"text": text})
            st["kept"] += 1
            st["added_tokens"] += _est_tokens(len(text))
        src_stats[src] = st
        print(f"[{src}] in={st['input_records']} kept={st['kept']} "
              f"exact_dup={st['dropped_exact_dup']} "
              f"gate_drop={st['dropped_by_keep_gate']} "
              f"near_flagged={st['near_dups_flagged']} "
              f"~+{st['added_tokens']:,} tok", flush=True)

    # mlx-clean-plus train = clean train ++ added; valid/test verbatim
    plus_train = clean_train + added_records
    _write(os.path.join(OUT_DIR, "train.jsonl"), plus_train)
    _write(os.path.join(OUT_DIR, "valid.jsonl"), clean_valid)
    _write(os.path.join(OUT_DIR, "test.jsonl"), clean_test)

    def _tok(recs):
        return _est_tokens(sum(len(r["text"]) for r in recs))

    clean_train_tok = _tok(clean_train)
    plus_train_tok = _tok(plus_train)
    stats = {
        "name": "mlx-clean-plus",
        "design": "valid/test copied verbatim from mlx-clean; extras appended to train only",
        "keep_rule": "prose (transcripts/.md): en AND quality_passed. "
                     "code (.swift/.h/.m/.mm/.c/.cpp/.metal): code-substance floor "
                     "(>=200 chars, >=8 non-blank lines, >=5 code-shaped lines) — "
                     "prose quality gate and langid both ignored for code, as the "
                     "Gopher alpha_words rule mislabels source syntax as junk. "
                     "Exact-dedup vs clean corpus + intra-extra; near-dups flagged not dropped.",
        "mlx_clean": {
            "train_records": len(clean_train),
            "valid_records": len(clean_valid),
            "test_records": len(clean_test),
            "train_tokens_est": clean_train_tok,
        },
        "mlx_clean_plus": {
            "train_records": len(plus_train),
            "valid_records": len(clean_valid),
            "test_records": len(clean_test),
            "train_tokens_est": plus_train_tok,
        },
        "added": {
            "records": len(added_records),
            "tokens_est": plus_train_tok - clean_train_tok,
            "per_source": src_stats,
        },
        "totals": {
            "exact_dups_dropped": sum(s["dropped_exact_dup"] for s in src_stats.values()),
            "keep_gate_dropped": sum(s["dropped_by_keep_gate"] for s in src_stats.values()),
            "near_dups_flagged": sum(s["near_dups_flagged"] for s in src_stats.values()),
        },
    }
    with open(os.path.join(OUT_DIR, "stats.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)

    print("\n=== mlx-clean-plus ===")
    print(f"train: {len(clean_train)} -> {len(plus_train)} records "
          f"(+{len(added_records)})")
    print(f"train tokens (est): {clean_train_tok:,} -> {plus_train_tok:,} "
          f"(+{plus_train_tok - clean_train_tok:,})")
    print(f"valid/test: {len(clean_valid)}/{len(clean_test)} (verbatim from mlx-clean)")
    print(f"exact dups dropped: {stats['totals']['exact_dups_dropped']}")
    print(f"near dups flagged:  {stats['totals']['near_dups_flagged']}")
    print(f"stats -> {os.path.relpath(os.path.join(OUT_DIR, 'stats.json'), _REPO_ROOT)}")
    return 0


def _write(path: str, recs: List[Dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps({"text": r["text"]}, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
