# /loop: Swift 6.4 / iOS 27 corpus drain

Run with: `/loop scrape swift64` (self-paced — no interval).

Each iteration:
1. `.venv/bin/python scripts/scrape_swift64_loop.py --list` — see remaining `[todo]` batches.
2. If none remain → report "drain complete" and STOP the loop (omit the next wakeup).
3. Otherwise run the next single batch: `.venv/bin/python scripts/scrape_swift64_loop.py`
   (no flags = next pending batch).

> The no-flag `/loop` unit still runs exactly one batch (nothing to fan out), so
> its behavior is unchanged. `--all` / `--batches` fan the scrape out across
> batches (≤ `--concurrency` at once, ≤ `--per-host` per host) and then run a
> single global SFT pass — a batch is marked `ok` once its build succeeds.

4. Verify the batch:
   - Confirm new lines under `data/corpus/rag/` and `data/corpus/sft/synthetic-sft.jsonl`.
   - `wc -l data/corpus/sft/synthetic-sft.jsonl` and sample the last record with
     `tail -1` — check `source_url`, `license_bucket`, `sft_origin: synthetic`, and
     that the answer preserves any version/availability detail.
   - For Apple DocC batches, framework files land at
     `data/corpus/rag/apple-framework/<framework>.jsonl` where `<framework>` is the
     lowercase URL path segment (e.g. `swiftui.jsonl`, `foundation.jsonl`, `swift.jsonl`).
5. Report: "batch <id> done — <R> rag files touched, <S> sft pairs total", then
   continue to the next iteration.

Stop early if a batch's state shows `"status": "failed"` twice — surface the error
instead of looping on it.

## CLI reference

```bash
# Show todo/done status for all batches
.venv/bin/python scripts/scrape_swift64_loop.py --list

# Plan without scraping (shows argv per batch)
.venv/bin/python scripts/scrape_swift64_loop.py --dry-run

# Run one specific batch by id
.venv/bin/python scripts/scrape_swift64_loop.py --batch apple-swiftui

# Drain every pending batch in one process (no /loop needed)
.venv/bin/python scripts/scrape_swift64_loop.py --all

# Fan out all pending batches in parallel (default --concurrency 3, --per-host 1)
.venv/bin/python scripts/scrape_swift64_loop.py --all --concurrency 4

# Fan out a named subset
.venv/bin/python scripts/scrape_swift64_loop.py --batches apple-swiftui,apple-foundation

# Skip the global synthetic-SFT pass
.venv/bin/python scripts/scrape_swift64_loop.py --all --no-sft
```

## Batches (from `scripts/swift64_manifest.yaml`)

| id | source | version hint |
|----|--------|--------------|
| `apple-swiftui` | appledocs-docc | iOS 27 / Swift 6.4 |
| `apple-foundation` | appledocs-docc | iOS 27 |
| `apple-swift-stdlib` | appledocs-docc | Swift 6.4 |
| `swift-evolution` | swift-evolution | Swift 6.4 |
| `wwdc-2026` | wwdc | iOS 27 |
| `swift-org-release-notes` | web | Swift 6.4 / Xcode 27 |

## Output layout

```
data/corpus/
  .loop_state.json                      # resumable state; each batch id → {status, ts, ...}
  rag/
    apple-framework/
      swiftui.jsonl                     # lowercase URL path segment
      foundation.jsonl
      swift.jsonl
    swift-evolution/
      <framework-or-general>.jsonl
    wwdc/
      <framework-or-general>.jsonl
    swift-book/
      <framework-or-general>.jsonl
  sft/
    synthetic-sft.jsonl                 # all batches append here; records carry sft_origin: "synthetic"
    _PROVENANCE.md                      # sidecar; documents source, generation method, date
```

Each SFT record carries:
- `source_url` — the page the pair was generated from
- `license_bucket` — e.g. `"apple-developer-docs-review-required"`
- `sft_origin: "synthetic"`

## End-to-end verification (user-run; requires live network + optional LLM backend)

> This step requires network access to Apple's developer docs. Run it manually after
> the loop session. An LLM backend is optional: without one, `generate_sft` emits
> only deterministic pairs.

```bash
# Configure backend (pick one):
export LOCAL_LLM_BASE_URL=http://localhost:1234/v1   # LM Studio / Ollama
# or: export ANTHROPIC_API_KEY=<key>

# Run one batch
.venv/bin/python scripts/scrape_swift64_loop.py --batch apple-swiftui
```

Confirm:
- `data/crawls/<stem>.json` was produced by the Apple scraper.
- `data/corpus/rag/apple-framework/swiftui.jsonl` exists.
- `data/corpus/sft/synthetic-sft.jsonl` has lines with `"sft_origin": "synthetic"`,
  a real `source_url`, and `"license_bucket": "apple-developer-docs-review-required"`.
- `data/corpus/.loop_state.json` shows `apple-swiftui` → `"status": "ok"`.
- A second `--batch apple-swiftui` run reports `skipped_seen` for already-processed
  records (idempotent).

## Notes

- Browser-tier scrapes (some `web`/SPA targets) need Docker; if a `web` batch
  yields empty results, note it and move on (HTTP-tier release-note pages still work).
- The standalone script is fully usable without the loop:
  `scripts/scrape_swift64_loop.py --all` drains everything in one process.
- `sft_include_restricted: true` in the manifest means Apple docs are included in
  synthetic SFT by default. Pass `--permissive-only` to `generate_sft.py` directly
  to skip restricted-license sources.
