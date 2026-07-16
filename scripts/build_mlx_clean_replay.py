#!/usr/bin/env python3
"""Build a general-Swift replay set + mixed DAPT corpus for replay-mixing CPT.

Inputs (READ-ONLY): data/dapt/mlx-clean/{train,valid,test}.jsonl  (canonical domain)
Replay sources:     /tmp/replay-repos/<repo>/**/*.swift           (general Swift)

Outputs (all under data/dapt/mlx-clean-replay/):
  train.jsonl, valid.jsonl, test.jsonl   -- domain + replay interleaved
  stats.json                             -- token counts, ratio, provenance
  replay_holdout.jsonl                   -- small held-out of general Swift
                                            (NOT in the mixed corpus; for the probe)

Token convention: len(text)//4  (project-wide).
"""
import hashlib
import json
import random
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOMAIN_DIR = REPO / "data/dapt/mlx-clean"
OUT_DIR = REPO / "data/dapt/mlx-clean-replay"
REPO_ROOT = Path("/tmp/replay-repos")

MIN_CHARS = 200
MAX_CHARS = 8000
TARGET_REPLAY_TOKENS = 900_000          # midpoint of 0.8-1.0M
HOLDOUT_GENERAL_RECS = 40               # small general-Swift held-out for the probe
SEED = 3407                             # match the training seed

# Large .swift files are CHUNKED into <=MAX_CHARS pieces (split on top-level
# blank-line boundaries) rather than discarded, so we keep every record within
# the 200-8000 char band without throwing away the bulk of real source.

# repo dir -> (license SPDX, source subdir to walk)
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
    # content hash on normalized whitespace so trivial reformat dups collapse
    norm = re.sub(r"\s+", " ", text).strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def strip_license_header(src: str) -> str:
    """Remove the leading contiguous block of // line comments + blank lines.

    Handles both the SwiftNIO //===...===// banner style and the Alamofire
    plain // block. Stops at the first line that is real code (or a /** doc).
    Only strips if that leading block actually looks like a license/copyright
    notice, so we don't nuke a legitimate leading doc comment on a code file.
    """
    lines = src.splitlines()
    i = 0
    n = len(lines)
    # consume leading blank lines
    while i < n and lines[i].strip() == "":
        i += 1
    start = i
    # consume the contiguous // comment block
    block = []
    while i < n:
        s = lines[i].strip()
        if s.startswith("//") or s == "":
            block.append(s)
            i += 1
            # don't run past a blank line that's followed by code
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
        return src  # leave it; it's a real doc comment, not a header
    # skip trailing blank lines after the header
    while i < n and lines[i].strip() == "":
        i += 1
    return "\n".join(lines[i:]).strip() + "\n"


def chunk_oversized(body: str) -> list:
    """Split a >MAX_CHARS source body into <=MAX_CHARS pieces on blank-line
    (paragraph / top-level decl) boundaries, falling back to a hard char split
    only if a single paragraph itself exceeds the cap. Returns a list of
    chunks, each already >=MIN_CHARS where possible (a trailing short remainder
    is merged back into the previous chunk)."""
    if len(body) <= MAX_CHARS:
        return [body]
    paras = re.split(r"\n\s*\n", body)
    chunks = []
    cur = ""
    for p in paras:
        piece = p.strip("\n")
        if not piece:
            continue
        # paragraph alone too big -> hard-split it
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
    # merge a too-short trailing chunk into its predecessor
    if len(chunks) >= 2 and len(chunks[-1]) < MIN_CHARS:
        merged = chunks[-2] + "\n\n" + chunks[-1]
        chunks = chunks[:-2] + [merged]
    return [c.strip() + "\n" for c in chunks if len(c.strip()) >= MIN_CHARS]


def load_domain_hashes() -> set:
    hs = set()
    for split in ("train", "valid", "test"):
        p = DOMAIN_DIR / f"{split}.jsonl"
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                hs.add(chash(d["text"]))
    return hs


def collect_replay(domain_hashes: set):
    """Walk repos, strip headers, filter by size, dedup. Returns list of dicts:
    {text, repo, license, path, tokens, hash}."""
    recs = []
    seen = set()  # dedup within replay too
    # deterministic file order
    for repo, (lic, subdir) in REPOS.items():
        base = REPO_ROOT / repo / subdir
        if not base.exists():
            base = REPO_ROOT / repo  # fallback: whole repo
        files = sorted(base.rglob("*.swift"))
        for fp in files:
            try:
                raw = fp.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            body = strip_license_header(raw)
            for ci, chunk in enumerate(chunk_oversized(body)):
                if not (MIN_CHARS <= len(chunk) <= MAX_CHARS):
                    continue
                h = chash(chunk)
                if h in domain_hashes:      # dedup against domain
                    continue
                if h in seen:               # dedup within replay
                    continue
                seen.add(h)
                rel = str(fp.relative_to(REPO_ROOT))
                recs.append({
                    "text": chunk,
                    "repo": repo,
                    "license": lic,
                    "path": rel if ci == 0 else f"{rel}#chunk{ci}",
                    "tokens": toks(chunk),
                    "hash": h,
                })
    return recs


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)

    print("Loading domain hashes...")
    domain_hashes = load_domain_hashes()
    print(f"  domain unique hashes: {len(domain_hashes)}")

    print("Collecting + cleaning replay .swift files...")
    pool = collect_replay(domain_hashes)
    print(f"  eligible replay records: {len(pool)} "
          f"({sum(r['tokens'] for r in pool):,} tokens available)")

    # Carve off a held-out general-Swift set FIRST (so it never enters the mix).
    rng.shuffle(pool)
    holdout = pool[:HOLDOUT_GENERAL_RECS]
    remaining = pool[HOLDOUT_GENERAL_RECS:]

    # Select replay records up to the token target from the remainder.
    rng.shuffle(remaining)
    replay = []
    acc = 0
    for r in remaining:
        if acc >= TARGET_REPLAY_TOKENS:
            break
        replay.append(r)
        acc += r["tokens"]
    replay_tokens = acc
    print(f"  selected replay records: {len(replay)} ({replay_tokens:,} tokens)")
    print(f"  general-Swift holdout:   {len(holdout)} "
          f"({sum(r['tokens'] for r in holdout):,} tokens)")

    # Load domain records (text only) for mixing.
    def load_split(split):
        out = []
        with open(DOMAIN_DIR / f"{split}.jsonl") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line)["text"])
        return out

    dom_train = load_split("train")
    dom_valid = load_split("valid")
    dom_test = load_split("test")
    domain_train_tokens = sum(toks(t) for t in dom_train)

    # Split replay into train/valid/test mirroring mlx-clean's ~1% style.
    # mlx-clean: 6025 train vs 61 valid / 61 test  -> ~1% each.
    n_rep = len(replay)
    n_rep_val = max(1, round(n_rep * 0.01))
    n_rep_test = max(1, round(n_rep * 0.01))
    rep_test = replay[:n_rep_test]
    rep_valid = replay[n_rep_test:n_rep_test + n_rep_val]
    rep_train = replay[n_rep_test + n_rep_val:]

    # Interleave domain + replay for TRAIN (so replay is spread through the run,
    # not clustered at the end). Deterministic shuffle of the merged list.
    train_records = [{"text": t} for t in dom_train] + [{"text": r["text"]} for r in rep_train]
    rng.shuffle(train_records)

    # valid/test = domain held-out + replay held-out, also interleaved.
    valid_records = [{"text": t} for t in dom_valid] + [{"text": r["text"]} for r in rep_valid]
    test_records = [{"text": t} for t in dom_test] + [{"text": r["text"]} for r in rep_test]
    rng.shuffle(valid_records)
    rng.shuffle(test_records)

    def write_jsonl(path, records):
        with open(path, "w") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    write_jsonl(OUT_DIR / "train.jsonl", train_records)
    write_jsonl(OUT_DIR / "valid.jsonl", valid_records)
    write_jsonl(OUT_DIR / "test.jsonl", test_records)

    # Held-out general-Swift set for the forgetting probe (text-only).
    write_jsonl(OUT_DIR / "replay_holdout.jsonl",
                [{"text": r["text"]} for r in holdout])

    # ---- stats ----
    rep_train_tokens = sum(r["tokens"] for r in rep_train)
    total_train_tokens = domain_train_tokens + rep_train_tokens
    ratio = rep_train_tokens / total_train_tokens

    # provenance: per-repo counts among SELECTED replay (train+valid+test)
    prov = {}
    for r in replay:
        e = prov.setdefault(r["repo"], {
            "license": r["license"], "url": GITHUB[r["repo"]],
            "records": 0, "tokens": 0,
        })
        e["records"] += 1
        e["tokens"] += r["tokens"]

    holdout_prov = {}
    for r in holdout:
        e = holdout_prov.setdefault(r["repo"], {"records": 0, "tokens": 0})
        e["records"] += 1
        e["tokens"] += r["tokens"]

    stats = {
        "description": "Replay-mixed DAPT corpus: clean Apple-audio domain + "
                       "general (non-audio) Swift replay, to mitigate "
                       "catastrophic forgetting on a small (~3.1M-token) domain.",
        "token_convention": "len(text)//4",
        "seed": SEED,
        "file_filter": {"min_chars": MIN_CHARS, "max_chars": MAX_CHARS,
                        "license_headers_stripped": True},
        "dedup": "content hash (sha256 over whitespace-normalized text); "
                 "replay deduped against domain AND within replay",
        "domain_source": "data/dapt/mlx-clean (canonical, read-only)",
        "tokens": {
            "domain_train": domain_train_tokens,
            "replay_train": rep_train_tokens,
            "total_train": total_train_tokens,
            "replay_ratio_train": round(ratio, 4),
            "replay_ratio_train_pct": round(ratio * 100, 2),
        },
        "records": {
            "train": {"domain": len(dom_train), "replay": len(rep_train),
                      "total": len(train_records)},
            "valid": {"domain": len(dom_valid), "replay": len(rep_valid),
                      "total": len(valid_records)},
            "test":  {"domain": len(dom_test), "replay": len(rep_test),
                      "total": len(test_records)},
        },
        "replay_provenance": prov,
        "general_swift_holdout": {
            "path": "data/dapt/mlx-clean-replay/replay_holdout.jsonl",
            "records": len(holdout),
            "tokens": sum(r["tokens"] for r in holdout),
            "note": "Held out BEFORE selection; never enters train/valid/test. "
                    "Used as the general-Swift eval set in the forgetting probe.",
            "provenance": holdout_prov,
        },
    }
    with open(OUT_DIR / "stats.json", "w") as f:
        json.dump(stats, f, indent=2)

    print("\n=== SUMMARY ===")
    print(f"domain_train_tokens : {domain_train_tokens:,}")
    print(f"replay_train_tokens : {rep_train_tokens:,}")
    print(f"total_train_tokens  : {total_train_tokens:,}")
    print(f"replay_ratio_train  : {ratio*100:.2f}%")
    print(f"train recs total    : {len(train_records)} "
          f"(domain {len(dom_train)} + replay {len(rep_train)})")
    print(f"valid recs total    : {len(valid_records)}")
    print(f"test recs total     : {len(test_records)}")
    print(f"general holdout recs: {len(holdout)}")
    print("Wrote:", OUT_DIR)


if __name__ == "__main__":
    main()
