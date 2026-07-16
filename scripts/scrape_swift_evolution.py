#!/usr/bin/env python3
"""
Scrape Swift Evolution proposals from GitHub for DAPT corpus.

Outputs:
  data/dapt/extra-sources/swift-evolution-proposals.jsonl
  data/crawls/swift-evolution/<SE-NNNN>.md  (one per proposal)
"""

import json
import os
import re
import sys
import time
from pathlib import Path

# Use curl_cffi for Chrome TLS impersonation (matches project's fetch.py style)
from curl_cffi import requests as cffi_requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REPO_CONTENTS_URL = (
    "https://api.github.com/repos/swiftlang/swift-evolution/contents/proposals"
)
RAW_BASE = "https://raw.githubusercontent.com/swiftlang/swift-evolution/main/proposals"

DATA_ROOT = Path(__file__).parent.parent / "data"
JSONL_OUT = DATA_ROOT / "dapt" / "extra-sources" / "swift-evolution-proposals.jsonl"
MD_OUT_DIR = DATA_ROOT / "crawls" / "swift-evolution"

SLEEP_BETWEEN = 0.3  # seconds between requests

# Concurrency proposals (by SE number)
CONCURRENCY_PROPOSALS = {
    296, 302, 306, 316, 317, 323, 338, 392, 401, 414, 430, 466,
}

HEADERS = {
    "Accept": "application/vnd.github.v3+json",
    "User-Agent": "swift-evolution-dapt-scraper/1.0",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def se_number(name: str) -> int | None:
    """Return the integer proposal number.

    Handles both filename formats:
      - Old GitHub format: SE-0392-foo.md  (SE- prefix)
      - Current format:    0392-foo.md     (no prefix, leading zeros)
    """
    # Match leading digits (with or without SE- prefix)
    m = re.match(r"(?:SE-)?0*(\d+)", name, re.IGNORECASE)
    return int(m.group(1)) if m else None


def is_high_value(name: str) -> bool:
    n = se_number(name)
    if n is None:
        return False
    # All proposals from SE-0400 onwards
    if n >= 400:
        return True
    # Named concurrency proposals
    if n in CONCURRENCY_PROPOSALS:
        return True
    return False


def extract_title(filename: str, content: str) -> str:
    """Build 'SE-NNNN: <human title>' from filename + first heading.

    Proposal headings are plain titles like:
        # Async/await
    with the SE number appearing separately in a * Proposal: [SE-0296] line.
    """
    n = se_number(filename)
    se_prefix = f"SE-{n:04d}" if n else ""

    # Try plain first-level heading (the human-readable title)
    m = re.search(r"^#\s+([^\n]+)", content, re.MULTILINE)
    if m:
        heading = m.group(1).strip()
        # If the heading already starts with SE-NNNN, use it directly
        if re.match(r"SE-\d+", heading):
            return heading
        return f"{se_prefix}: {heading}" if se_prefix else heading

    # Fallback to filename stem
    stem = re.sub(r"^\d+-", "", filename.replace(".md", ""))
    return f"{se_prefix}: {stem}" if se_prefix else stem


def fetch_json(url: str) -> dict | list:
    resp = cffi_requests.get(url, headers=HEADERS, impersonate="chrome110", timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_text(url: str) -> str:
    resp = cffi_requests.get(url, headers=HEADERS, impersonate="chrome110", timeout=30)
    resp.raise_for_status()
    return resp.text


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    MD_OUT_DIR.mkdir(parents=True, exist_ok=True)
    JSONL_OUT.parent.mkdir(parents=True, exist_ok=True)

    print("Fetching proposal directory listing from GitHub API…")
    listing = fetch_json(REPO_CONTENTS_URL)
    print(f"  Total files in proposals/: {len(listing)}")

    # Filter to .md files only, then to high-value ones
    md_files = [f for f in listing if f["name"].endswith(".md")]
    selected = [f for f in md_files if is_high_value(f["name"])]
    selected.sort(key=lambda f: se_number(f["name"]) or 0)

    print(f"  Selected {len(selected)} high-value proposals")

    downloaded = []
    failures = []

    for i, entry in enumerate(selected, 1):
        name = entry["name"]
        # Prefer download_url (raw), fall back to constructing it
        raw_url = entry.get("download_url") or f"{RAW_BASE}/{name}"
        n = se_number(name)
        se_label = f"SE-{n:04d}" if n else name.replace(".md", "")
        github_url = f"https://github.com/swiftlang/swift-evolution/blob/main/proposals/{name}"

        print(f"  [{i:3d}/{len(selected)}] {name}… ", end="", flush=True)

        try:
            content = fetch_text(raw_url)
            title = extract_title(name, content)

            # Save individual .md — use SE-NNNN-<rest>.md for clarity
            # If file already has SE- prefix use it; otherwise prepend SE-NNNN- stem
            if name.upper().startswith("SE-"):
                out_name = name
            else:
                out_name = f"{se_label}-{name}"
            md_path = MD_OUT_DIR / out_name
            md_path.write_text(content, encoding="utf-8")

            downloaded.append({
                "filename": out_name,
                "number": n,
                "title": title,
                "chars": len(content),
                "url": github_url,
            })
            print(f"ok  ({len(content):,} chars)")

        except Exception as exc:
            print(f"FAIL — {exc}")
            failures.append({"filename": name, "error": str(exc)})

        if i < len(selected):
            time.sleep(SLEEP_BETWEEN)

    # Write JSONL
    print(f"\nWriting JSONL to {JSONL_OUT}…")
    with JSONL_OUT.open("w", encoding="utf-8") as fh:
        for item in downloaded:
            # Re-read the file we just saved so we don't hold all content in RAM
            content = (MD_OUT_DIR / item["filename"]).read_text(encoding="utf-8")
            record = {
                "text": content,
                "meta": {
                    "url": item["url"],
                    "title": item["title"],
                    "source": "swift-evolution",
                    "proposal_number": item["number"],
                },
            }
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    # Summary
    total_chars = sum(d["chars"] for d in downloaded)
    numbers = sorted(d["number"] for d in downloaded)

    print("\n" + "=" * 60)
    print(f"Downloaded : {len(downloaded)} proposals")
    print(f"Failures   : {len(failures)}")
    print(f"Total chars: {total_chars:,}")
    print(f"JSONL      : {JSONL_OUT}")
    print(f"MD dir     : {MD_OUT_DIR}")
    print()
    print("Proposal numbers included:")
    # Print in rows of 10
    for row_start in range(0, len(numbers), 10):
        row = numbers[row_start:row_start + 10]
        print("  " + "  ".join(f"SE-{n:04d}" for n in row))

    if failures:
        print("\nFailures:")
        for f in failures:
            print(f"  {f['filename']}: {f['error']}")

    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
