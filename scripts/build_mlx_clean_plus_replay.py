#!/usr/bin/env python3
"""Join step: build the FINAL CPT corpus = enlarged domain (mlx-clean-plus)
+ general-Swift replay, at the same replay ratio proven in mlx-clean-replay.

This combines the two parallel handoffs:
  * Handoff 2 produced the enlarged domain  data/dapt/mlx-clean-plus  (mlx-clean
    + WWDC transcripts + sample-code source, +726k tokens / +23%).
  * Handoff 1 proved replay-mixing at ~22.13% replay tokens prevents
    catastrophic forgetting of general Swift on a small domain.

We re-run Handoff 1's exact replay extraction (same /tmp/replay-repos clones,
same chunking / header-strip / dedup), but:
  - dedup replay against the ENLARGED domain (mlx-clean-plus), not mlx-clean;
  - EXCLUDE the same 40-record general-Swift holdout used in Handoff 1's probe
    (so the combined-corpus probe stays comparable and the eval set never leaks);
  - select replay to hit the SAME 22.13% ratio against the now-larger domain
    (~1.09M replay tokens vs the 0.88M used against the smaller domain);
  - keep valid/test PURE DOMAIN (verbatim from mlx-clean-plus) so the in-training
    val curve is directly comparable to the no-replay mlx-clean-plus baseline.

Inputs (READ-ONLY):
  data/dapt/mlx-clean-plus/{train,valid,test}.jsonl   (enlarged domain)
  data/dapt/mlx-clean-replay/replay_holdout.jsonl     (general-Swift holdout, reused)
  /tmp/replay-repos/<repo>/**/*.swift                 (general Swift replay sources)

Outputs (data/dapt/mlx-clean-plus-replay/):
  train.jsonl  -- plus-domain train + replay, interleaved (replay in TRAIN only)
  valid.jsonl  -- verbatim mlx-clean-plus valid (pure domain)
  test.jsonl   -- verbatim mlx-clean-plus test  (pure domain)
  stats.json   -- token counts, ratio, provenance, build provenance

Token convention: len(text)//4 (project-wide).
"""
import hashlib
import json
import random
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOMAIN_DIR = REPO / "data/dapt/mlx-clean-plus"            # enlarged domain
HOLDOUT_FILE = REPO / "data/dapt/mlx-clean-replay/replay_holdout.jsonl"
OUT_DIR = REPO / "data/dapt/mlx-clean-plus-replay"
REPO_ROOT = Path("/tmp/replay-repos")

MIN_CHARS = 200
MAX_CHARS = 8000
TARGET_REPLAY_RATIO = 0.2213             # match mlx-clean-replay's proven ratio
SEED = 3407                              # match the training seed

# repo dir -> (license SPDX, source subdir to walk)  -- same set as Handoff 1
REPOS = {
    "swift-algorithms":     ("Apache-2.0", "Sources"),
    "swift-collections":    ("Apache-2.0", "Sources"),
    "swift-nio":            ("Apache-2.0", "Sources"),
    "swift-numerics":       ("Apache-2.0", "Sources"),
    "swift-system":         ("Apache-2.0", "Sources"),
    "swift-argument-parser":("Apache-2.0", "Sources"),
    "Alamofire":            ("MIT",        "Source"),
}
GITHUB = {
    "swift-algorithms":      "https://github.com/apple/swift-algorithms",
    "swift-collections":     "https://github.com/apple/swift-collections",
    "swift-nio":             "https://github.com/apple/swift-nio",
    "swift-numerics":        "https://github.com/apple/swift-numerics",
    "swift-system":          "https://github.com/apple/swift-system",
    "swift-argument-parser": "https://github.com/apple/swift-argument-parser",
    "Alamofire":             "https://github.com/Alamofire/Alamofire",
}


def toks(text: str) -> int:
    return len(text) // 4


def chash(text: str) -> str:
    norm = re.sub(r"\s+", " ", text).strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def strip_license_header(src: str) -> str:
    lines = src.splitlines()
    i, n = 0, len(lines)
    while i < n and lines[i].strip() == "":
        i += 1
    block = []
    while i < n:
        s = lines[i].strip()
        if s.startswith("//") or s == "":
            block.append(s)
            i += 1
            continue
        break
    blocktext = "\n".join(block).lower()
    looks_like_license = any(
        k in blocktext for k in
        ("copyright", "licensed under", "license information",
         "spdx-license-identifier", "permission is hereby granted",
         "open source project")
    )
    if not looks_like_license:
        return src
    while i < n and lines[i].strip() == "":
        i += 1
    return "\n".join(lines[i:]).strip() + "\n"


def chunk_oversized(body: str) -> list:
    if len(body) <= MAX_CHARS:
        return [body]
    paras = re.split(r"\n\s*\n", body)
    chunks, cur = [], ""
    for p in paras:
        piece = p.strip("\n")
        if not piece:
            continue
        if len(piece) > MAX_CHARS:
            if cur:
                chunks.append(cur)
                cur = ""
            for k in range(0, len(piece), MAX_CHARS):
                chunks.append(piece[k:k + MAX_CHARS])
            continue
        candidate = (cur + "\n\n" + piece) if cur else piece
        if len(candidate) > MAX_CHARS:
            chunks.append(cur)
            cur = piece
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    if len(chunks) >= 2 and len(chunks[-1]) < MIN_CHARS:
        merged = chunks[-2] + "\n\n" + chunks[-1]
        chunks = chunks[:-2] + [merged]
    return [c.strip() + "\n" for c in chunks if len(c.strip()) >= MIN_CHARS]


def load_hashes(jsonl_path: Path) -> set:
    hs = set()
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if line:
                hs.add(chash(json.loads(line)["text"]))
    return hs


def load_texts(jsonl_path: Path) -> list:
    out = []
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line)["text"])
    return out


def collect_replay(domain_hashes: set, preseed_seen: set):
    """Walk repos, strip headers, chunk, filter, dedup against domain AND the
    preseed_seen set (which carries the holdout hashes so they never re-enter)."""
    recs = []
    seen = set(preseed_seen)            # holdout hashes excluded up-front
    for repo, (lic, subdir) in REPOS.items():
        base = REPO_ROOT / repo / subdir
        if not base.exists():
            base = REPO_ROOT / repo
        for fp in sorted(base.rglob("*.swift")):
            try:
                raw = fp.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            body = strip_license_header(raw)
            for ci, chunk in enumerate(chunk_oversized(body)):
                if not (MIN_CHARS <= len(chunk) <= MAX_CHARS):
                    continue
                h = chash(chunk)
                if h in domain_hashes or h in seen:
                    continue
                seen.add(h)
                rel = str(fp.relative_to(REPO_ROOT))
                recs.append({
                    "text": chunk, "repo": repo, "license": lic,
                    "path": rel if ci == 0 else f"{rel}#chunk{ci}",
                    "tokens": toks(chunk), "hash": h,
                })
    return recs


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)

    print("Loading enlarged-domain (mlx-clean-plus) hashes + holdout...")
    domain_hashes = set()
    for split in ("train", "valid", "test"):
        domain_hashes |= load_hashes(DOMAIN_DIR / f"{split}.jsonl")
    holdout_hashes = load_hashes(HOLDOUT_FILE)
    print(f"  domain unique hashes: {len(domain_hashes)}  | holdout: {len(holdout_hashes)}")

    print("Collecting + cleaning replay .swift files...")
    pool = collect_replay(domain_hashes, holdout_hashes)
    pool_tokens = sum(r["tokens"] for r in pool)
    print(f"  eligible replay records: {len(pool)} ({pool_tokens:,} tokens available, "
          f"holdout + domain already excluded)")

    dom_train = load_texts(DOMAIN_DIR / "train.jsonl")
    dom_valid = load_texts(DOMAIN_DIR / "valid.jsonl")
    dom_test = load_texts(DOMAIN_DIR / "test.jsonl")
    domain_train_tokens = sum(toks(t) for t in dom_train)

    # target replay tokens to hit the proven ratio against the enlarged domain:
    #   r = replay / (domain + replay)  ->  replay = r/(1-r) * domain
    target = round(domain_train_tokens * TARGET_REPLAY_RATIO / (1 - TARGET_REPLAY_RATIO))
    print(f"  domain_train_tokens={domain_train_tokens:,}  target_replay_tokens={target:,}")
    if pool_tokens < target:
        print(f"  WARNING: pool ({pool_tokens:,}) < target ({target:,}); using whole pool")

    rng.shuffle(pool)
    replay, acc = [], 0
    for r in pool:
        if acc >= target:
            break
        replay.append(r)
        acc += r["tokens"]
    replay_tokens = acc
    print(f"  selected replay records: {len(replay)} ({replay_tokens:,} tokens)")

    # replay goes into TRAIN only; valid/test stay pure-domain (verbatim).
    train_records = [{"text": t} for t in dom_train] + [{"text": r["text"]} for r in replay]
    rng.shuffle(train_records)
    valid_records = [{"text": t} for t in dom_valid]
    test_records = [{"text": t} for t in dom_test]

    def write_jsonl(path, records):
        with open(path, "w") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    write_jsonl(OUT_DIR / "train.jsonl", train_records)
    write_jsonl(OUT_DIR / "valid.jsonl", valid_records)
    write_jsonl(OUT_DIR / "test.jsonl", test_records)

    total_train_tokens = domain_train_tokens + replay_tokens
    ratio = replay_tokens / total_train_tokens

    prov = {}
    for r in replay:
        e = prov.setdefault(r["repo"], {
            "license": r["license"], "url": GITHUB[r["repo"]],
            "records": 0, "tokens": 0})
        e["records"] += 1
        e["tokens"] += r["tokens"]

    stats = {
        "name": "mlx-clean-plus-replay",
        "description": "FINAL CPT corpus: enlarged Apple-audio domain (mlx-clean-plus) "
                       "+ general (non-audio) Swift replay at the proven ~22% ratio. "
                       "Joins Handoff 2 (dense additive sources) with Handoff 1 (replay-mixing).",
        "token_convention": "len(text)//4",
        "seed": SEED,
        "domain_source": "data/dapt/mlx-clean-plus (mlx-clean + WWDC transcripts + sample-code source)",
        "replay_sources": "/tmp/replay-repos (7 permissive general Swift repos; see provenance)",
        "design": "replay interleaved into TRAIN only; valid/test verbatim from mlx-clean-plus "
                  "(pure domain) so the val curve is directly comparable to the no-replay baseline",
        "holdout_reused": str(HOLDOUT_FILE.relative_to(REPO)),
        "dedup": "content hash (sha256 over whitespace-normalized text); replay deduped against "
                 "the ENLARGED domain AND within replay; the 40-rec general-Swift holdout excluded up-front",
        "target_replay_ratio": TARGET_REPLAY_RATIO,
        "tokens": {
            "domain_train": domain_train_tokens,
            "replay_train": replay_tokens,
            "total_train": total_train_tokens,
            "replay_ratio_train": round(ratio, 4),
            "replay_ratio_train_pct": round(ratio * 100, 2),
            "eligible_replay_pool": pool_tokens,
        },
        "records": {
            "train": {"domain": len(dom_train), "replay": len(replay), "total": len(train_records)},
            "valid": {"domain": len(dom_valid), "replay": 0, "total": len(valid_records)},
            "test":  {"domain": len(dom_test), "replay": 0, "total": len(test_records)},
        },
        "replay_provenance": prov,
    }
    with open(OUT_DIR / "stats.json", "w") as f:
        json.dump(stats, f, indent=2)

    print("\n=== SUMMARY ===")
    print(f"domain_train_tokens : {domain_train_tokens:,}")
    print(f"replay_train_tokens : {replay_tokens:,}")
    print(f"total_train_tokens  : {total_train_tokens:,}")
    print(f"replay_ratio_train  : {ratio*100:.2f}%")
    print(f"train recs total    : {len(train_records)} (domain {len(dom_train)} + replay {len(replay)})")
    print(f"valid recs total    : {len(valid_records)} (pure domain)")
    print(f"test recs total     : {len(test_records)} (pure domain)")
    print("Wrote:", OUT_DIR)


if __name__ == "__main__":
    main()
