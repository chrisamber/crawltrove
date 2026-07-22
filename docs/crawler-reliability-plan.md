# CrawlTrove crawler reliability implementation plan

**Inputs:** `crawltrove-engineering-spec.md` and
`frontier-scraping-evaluation.md`

**Baseline:** CrawlTrove v0.3.0 (`origin/main`)

**Release goal:** fix the measured acquisition defects, then replace the
process-local crawl frontier with the smallest durable implementation.

## Assumptions

- **Load-bearing:** the supported deployment is one self-hosted Docker Compose
  stack on one host.
- **Load-bearing:** public crawls remain bounded to 100 pages and depth 5 for
  this release.
- **Load-bearing:** PostgreSQL is available in the supported runtime and is the
  source of truth for active crawl state.
- Browser acquisition is exceptional; ordinary HTML and non-HTML resources use
  the HTTP tier.
- Execution is at least once. Database effects are idempotent; duplicate network
  requests may occur after a worker loses its lease.
- This release improves reliability and acquisition correctness. It does not
  attempt to reproduce a proxy network, CAPTCHA service, or hosted browser fleet.

If the deployment becomes multi-host, the crawl limit grows materially beyond
100 pages, or protected-site acquisition becomes a paid product requirement,
revisit the deferred items below instead of expanding this release.

## Release boundary

Ship two increments in order:

1. **Acquisition hotfix:** remove the three measured P0 defects without changing
   the crawl control plane.
2. **Durable crawler:** persist the frontier in PostgreSQL and run it from one
   independently restartable worker using the existing API and storage model.

Do not combine the hotfix with the database migration. It is independently
valuable, small enough to verify directly, and reduces uncertainty before the
control-plane change.

## Simplest design

```text
client
  |
  v
FastAPI API -------- PostgreSQL -------- crawl worker
  |                      |                    |
  |                      |                    +-- shared curl_cffi session
  |                      |                    +-- one reused Chromium process
  |                      |
  +-------------- existing filesystem artifacts
```

The API and worker use the same image and repository code. Compose runs them as
separate processes because crawl work must survive API termination. The worker
also claims scheduled definitions, reaps expired leases, and finalizes runs;
there is no separate scheduler or maintenance service.

### Data model

Reuse the existing tables:

- `scrape_runs` remains the job execution record. Add effective crawl config,
  `terminal_reason`, `deadline_at`, and `cancel_requested_at`.
- `scraped_pages` remains the result table. Add nullable `crawl_task_id` with a
  unique constraint so repeated completion cannot create duplicate results.
- `scrape_errors` remains the detailed failure log.

Add only:

- `crawl_tasks`: `id`, `run_id`, original and normalized URL, URL hash,
  `origin_key`, depth, discovery order, state, `available_at`, attempt count,
  lease owner/token/expiry, HTTP status, failure code/message, and timestamps.
  Enforce uniqueness on `(run_id, url_hash)` and index the claim order.
- `crawl_origins`: `origin_key`, cached robots data and expiry,
  `next_request_at`, and `blocked_until`.

Task states are `pending`, `leased`, `succeeded`, `failed`, and `cancelled`.
Retries return to `pending` with a future `available_at`. Robots, HTTP,
extraction, challenge, timeout, and retry-exhaustion outcomes are stable failure
codes, not additional states.

Run states are `pending`, `processing`, `completed`, `partial`, `failed`, and
`cancelled`. A deadline is `terminal_reason=deadline`, allowing clients to
distinguish it without another transition graph.

Status counts are computed from indexed task rows. Do not add cached counters or
a reconciliation command while a run is limited to 100 tasks.

### API contract

Keep the existing routes and evolve them additively:

- `POST /api/crawl` validates the request, inserts `scrape_runs` and seed
  `crawl_tasks` in one transaction, then returns `202`.
- `GET /api/crawl/{job_id}` reads durable run state and grouped task counts.
- `POST /api/crawl/{job_id}/cancel` records cancellation; workers stop new
  claims and cancel remaining pending tasks.
- Keep `/resume` only for legacy checkpoint jobs during the transition; durable
  jobs recover automatically through lease expiry.

Do not introduce parallel `/api/crawls` routes or a deprecation cycle in this
release.

### Worker flow

1. Claim one eligible task with `FOR UPDATE SKIP LOCKED`; assign a random lease
   token and expiry.
2. Lock or create its `crawl_origins` row. Apply robots, minimum delay, and
   `blocked_until`. If not eligible, return the task to `pending` without
   incrementing its attempt count.
3. Fetch through the shared HTTP tier. Escalate to Chromium only for successful
   HTML that structurally needs rendering.
4. Stream into a per-response byte cap. Never materialize an oversized response.
5. Extract content and discover links from the uncleaned HTTP or rendered HTML.
6. In one transaction, verify the lease token, insert the page idempotently,
   insert ordered child tasks with `ON CONFLICT DO NOTHING`, and finish the task.
7. Requeue retryable failures with bounded exponential backoff. Honor
   `Retry-After`; never escalate `429` or `503` to Chromium.
8. Finalize the run when no nonterminal task remains. Any successful page plus
   an unexpected permanent failure produces `partial`, not `completed`.

The worker renews leases during browser navigation and document extraction. A
stale worker cannot commit because completion matches both task ID and lease
token. Periodic worker maintenance returns expired tasks to `pending` or fails
them after the attempt limit.

## Implementation order

### 1. Acquisition hotfix

Fix and verify one defect at a time, reporting `Bug N/3 fixed.` after its focused
test passes.

1. Replace waiting Playwright metadata locators with immediate DOM reads.
2. Reuse the existing challenge markers after rendering and return an explicit
   `blocked_challenge` failure before cleaning removes evidence.
3. Restrict automatic browser escalation to HTML; replace the fixed short-text
   rule with shell/challenge structure checks.

Leave one local-browser regression test covering absent metadata, short complete
HTML, plain text, a JavaScript shell, and a rendered challenge. It must not call
external sites.

Acceptance:

- The absent-metadata browser fixture completes in under 5 seconds.
- Complete short HTML stays on HTTP.
- Text, Markdown, JSON, XML, and feeds never launch Chromium automatically.
- A rendered challenge is not returned or indexed as successful content.

### 2. Durable repository

- Add the two tables and extensions above in one forward migration.
- Add repository operations for submit, claim, heartbeat, complete, retry,
  cancel, expire, finalize, and grouped status.
- Use existing normalization and page-row mapping helpers.
- Add one PostgreSQL integration test proving lease-token fencing and idempotent
  page completion.

### 3. Independent worker and compatibility API

- Add a worker entry point using the repository operations.
- Change `POST /api/crawl` from `BackgroundTasks` execution to transactional
  submission.
- Move scheduled crawl execution into durable task submission.
- Add the worker to Compose using the existing image.
- Read durable status from the existing polling endpoint and add cancellation.

Acceptance:

- Killing the API after `202` does not stop the crawl.
- Killing a worker mid-fetch allows the task to recover after lease expiry.
- Two workers cannot persist duplicate results.
- Cancellation prevents new claims.

### 4. Politeness and resource bounds

- Use `urllib.robotparser.RobotFileParser` for robots rule selection while
  retaining CrawlTrove's SSRF-safe fetch path.
- Serialize origin eligibility with the `crawl_origins` row. Count unexpired
  leased tasks by `origin_key`; do not add an origin-lease table.
- Reuse one `curl_cffi.AsyncSession` per worker.
- Enforce transferred and decoded response caps before extraction.
- Add Retry-After and bounded exponential backoff; omit a circuit breaker.

Acceptance:

- Two jobs targeting one origin observe concurrency 1 and the configured delay.
- Robots denial prevents page fetch.
- `429` and `503` never launch Chromium.
- An oversized fixture terminates before its full body is retained.

### 5. Browser reuse and deterministic discovery

- Let the existing `WebScraper` own one Playwright runtime and Chromium process;
  do not introduce a manager interface.
- Create a fresh context per task and close it in `finally`.
- Return raw discovery HTML separately from cleaned content HTML.
- Preserve document order with insertion-ordered deduplication; remove
  `list(set(...))` traversal.
- Discover ordinary `<a href>` links plus existing sitemap entries. Retain
  linked PDF and EPUB URLs when document ingestion is enabled.

Acceptance:

- Many browser tasks launch one Chromium process, excluding crash recovery.
- Navigation outside `<main>` still contributes links.
- Repeated fixture crawls produce the same frontier order.

### 6. Release verification and cleanup

- Remove the legacy process-local frontier after compatibility tests pass.
- Run focused tests sequentially while iterating, then clear relevant caches and
  run the complete Python, dashboard, Compose, and Docker checks.
- Start the supported Compose stack and prove the API, worker, Postgres,
  dashboard, and `/api/health` response before reporting success.
- Record queue depth, task outcomes, retries, origin-delay time, and task
  duration. No other metrics are release requirements.

## Integrity and failure rules

- Submission commits before returning `202`.
- Completion is accepted only for the current lease token.
- Child-task insertion and parent completion share one transaction.
- A URL is unique per run at the database boundary.
- Redirects and browser requests retain existing SSRF validation.
- TLS verification and the Chromium sandbox remain enabled by default.
- Challenge pages and non-2xx responses are never successful pages.
- Artifact failure cannot silently turn an unpersisted page into success; the
  task records a stable failure code and can retry when appropriate.

## Deferred deliberately

- `crawl_events`, server-sent events, retry-failed job cloning, idempotency keys.
- S3 storage and an `ArtifactStore` interface.
- Dedicated scheduler, maintenance, reaper, or finalizer services.
- Circuit-breaker states and half-open probes.
- Persistent browser profiles, user cookies, proxies, CAPTCHA solving, and
  third-party acquisition adapters.
- `same_site`, allowlist, and unrestricted cross-origin crawling.
- Crawl-trap heuristics beyond hard page, depth, URL-length, redirect, and
  response-size limits.
- A 100,000-task/eight-worker load gate, cached counters, and reconciliation.
- A large environment-variable matrix, full admin command suite, or 18-metric
  catalog.

## Trade-offs and flip conditions

| Choice | Why now | Revisit when |
| --- | --- | --- |
| One worker process | Current crawls are capped at 100 pages. | Pending-task age exceeds 30 seconds for 15 minutes while worker CPU exceeds 70%. |
| Filesystem artifacts | Supported deployment is one host. | Workers run on separate hosts or artifact availability must survive host loss. |
| Database aggregates | At most 100 task rows per crawl. | Status-query p95 exceeds 100 ms under measured production load. |
| No circuit breaker | Retry-After and backoff cover known failure behavior. | One origin produces more than 100 retryable failures in five minutes despite backoff. |
| No proxy/provider layer | No measured requirement to acquire protected sites. | Blocked outcomes exceed an agreed product SLO for business-critical targets. |
| Same-origin crawl only | Matches the existing bounded product surface. | A concrete corpus requires controlled cross-origin traversal. |

## Definition of done

- All three measured P0 defects have local regression coverage and pass.
- Accepted jobs survive API termination and worker termination.
- Lease fencing, idempotent completion, cancellation, retries, robots, origin
  delay, and response caps pass integration tests.
- Existing scrape, document, extraction, retrieval, and SSRF tests remain green.
- The dashboard reports `completed`, `partial`, `failed`, and `cancelled`
  accurately through the existing API.
- The supported Compose deployment proves healthy with one API, one worker, and
  PostgreSQL.
