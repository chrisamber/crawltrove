# Deep research — spec

`POST /api/research` runs an autonomous **plan → search → read → assess →
synthesize** loop and produces a cited Markdown report. This document is the
spec that `CLAUDE.md` and `app/research.py` reference: Phase 1 is the loop;
Phase 2 (checkpoint/resume) makes in-flight runs survive restarts.

## Phase 1 — the loop

Implemented in [app/research.py](../app/research.py) (`ResearchManager`, a
shared singleton in [app/services.py](../app/services.py)).

1. **Plan** — `research_llm.plan_queries` turns the question into up to 4
   search queries.
2. Per round (up to `maxRounds`):
   - **Search** — every query goes through [app/search.py](../app/search.py)
     (`SEARXNG_BASE_URL` → `BRAVE_SEARCH_API_KEY` → keyless DuckDuckGo).
   - **Select** — `research_llm.select_urls` picks up to 6 unread candidates,
     bounded by the remaining page budget. Caller-supplied `seedUrls` are
     read first.
   - **Read** — each page goes through the normal `WebScraper.scrape()`
     waterfall and is saved with `storage.save_scrape`, so research pages
     carry the usual corpus signals and citations point at real `data/`
     artifacts. `research_llm.take_notes` extracts relevance + key facts.
   - **Assess** — `research_llm.assess` decides *enough* or emits new queries.
3. **Synthesize** — always runs, even on failure/cancel/budget-stop, so every
   terminal run has a report (partial if needed). Quality scores rank the
   synthesis input; nothing is dropped (signals flag, never filter).
   `validate_citations` flags citation indexes with no matching source.

**Budgets** (all enforced centrally): `maxRounds` (1–10, default 4),
`maxPages` (1–100, default 25), `maxMinutes` (1–120, default 30), and an LLM
call cap (40; synthesis is exempt so a capped run still reports). At most
**2 concurrent runs** (`MAX_CONCURRENT`); excess requests get `429`.

**Job state** is one JSON-serializable dict per run (status, budgets,
counters, `activity` log, `sources`, `report`, …), browsable live via
`GET /api/research/{jobId}` and saved to `data/research/<stem>.json|.md` at
terminal. `POST /api/research/{jobId}/cancel` winds a run down to a partial
report.

## Phase 2 — checkpoint/resume

The job store is in-memory, but every in-flight run is checkpointed to
**`data/research/checkpoints/<job_id>.json`** so a restart no longer loses
it. Files are the source of truth — none of this requires `DATABASE_URL`.

### Checkpoint format (version 1)

```json
{
  "version": 1,
  "job": { "...": "the full job dict, verbatim" },
  "loop": {
    "round_no": 2,
    "phase": "read",
    "queries": ["..."],
    "pending": ["https://..."],
    "seen": ["https://..."]
  },
  "updated_at": "2026-07-10T12:20:00+00:00"
}
```

`loop` captures the three values that used to live only in the loop frame:
the current `queries`, the `pending` URLs still to read, and the `seen` set
(serialized sorted). `phase` is `"search"` (next step: search+select for
`round_no`+1) or `"read"` (mid-round; drain `pending` first).

### Write sites

Checkpoints are atomic (`.tmp` + `os.replace`) and rewritten: at run start,
after planning, after each round's select, **before and after each page
read**, and after each assess. The pre-read write *claims* the URL (already
popped from `pending`, added to `seen`), so a crash mid-read skips that URL
on resume instead of crash-looping on it. The whole job dict rides along in
the same write, so counters (`pages_scraped`, `llm_calls`) and `sources` are
always consistent with the loop state — a resume never re-reads a page or
double-counts a budget. Checkpoint writes swallow their own failures
(resilient-signal rule: a checkpoint error never fails the run).

### Deadline across restarts

`run_research` stamps a wall-clock `deadline_utc` on the job (now +
`maxMinutes`) and derives the monotonic deadline `_check()` polls from
whatever remains. A resume past the deadline skips straight to synthesis, so
the run still terminates with a report.

### Restore and resume

- **Startup** ([app/main.py](../app/main.py), *not* gated on `DATABASE_URL`):
  `restore_from_checkpoints()` rehydrates every non-terminal checkpoint into
  the job store with status **`interrupted`** (terminal leftovers — a crash
  between artifact save and checkpoint delete — are cleaned up). When
  `RESEARCH_RESUME_ON_START` (default `true`) is on and an LLM backend is
  configured, the newest interrupted runs auto-resume up to `MAX_CONCURRENT`.
- **`POST /api/research/{jobId}/resume`** — explicit resume. `404` unknown,
  `409` not interrupted, `501` no LLM backend, `429` at the cap.
- `interrupted` jobs are idle: they are excluded from `active_jobs()` so they
  never starve the concurrency cap.
- Resumed runs are dispatched with `asyncio.create_task` + a strong-ref set
  (the runner pattern) — they are not tied to any request.

The checkpoint is deleted (best-effort) once `storage.save_research` has
written the terminal artifact. `storage.prune` covers `data/research/`
artifact pairs like scrapes/crawls, and sweeps checkpoints older than the
cutoff (a non-terminal checkpoint that old is a dead run).

### Optional DB index

Migration `0003_research_runs.sql` adds a thin **`research_runs`** table
(`job_id` unique; query, status, counters, artifact stem, timestamps),
upserted at run start and terminal via `repo.upsert_research_run`. It is an
ops-queryable index only — never read for resume — and follows the repo
rules: no-op without `DATABASE_URL`, every call swallowed.

## Environment

| Var | Default | Effect |
| --- | --- | --- |
| `RESEARCH_RESUME_ON_START` | `true` | Auto-resume interrupted runs at startup |
| `SEARXNG_BASE_URL` / `BRAVE_SEARCH_API_KEY` | unset | Search provider waterfall (else DuckDuckGo) |
| `LOCAL_LLM_BASE_URL` / `ANTHROPIC_API_KEY` / `AI_GATEWAY_API_KEY` | unset | LLM backend waterfall (shared with `/api/extract`) |
