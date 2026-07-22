# CrawlTrove Crawler Reliability and Scale Engineering Specification

**Document ID:** CT-ENG-001

**Status:** Draft for implementation

**Baseline:** CrawlTrove v0.3.0 audited repository state

**Audience:** Backend, platform, security, and QA engineers
**Primary release scope:** P0 and P1 crawler-control-plane remediation

## 1. Purpose

This specification defines the engineering changes required to make CrawlTrove:

1. Durable across process and host failures.
2. Safe to run with multiple API processes and crawler workers.
3. Polite to target sites across all jobs and workers.
4. Resource-bounded under large, malformed, or hostile responses.
5. Deterministic and complete enough for reliable website discovery.
6. Explicit about partial, failed, cancelled, and timed-out outcomes.
7. Observable and operable in production.

The existing extraction, corpus-generation, document-processing, metadata, and SSRF controls remain core product differentiators. This work replaces the process-local crawler control plane without replacing the extraction pipeline.

Normative terms **MUST**, **SHOULD**, and **MAY** define required, recommended, and optional behavior.

---

## 2. Scope

### 2.1 In scope

- Durable crawl jobs and page tasks in PostgreSQL.
- Lease-based execution by one or more independent workers.
- Host-aware concurrency, delay, robots.txt handling, retries, and circuit breaking.
- Shared HTTP connections and long-lived Playwright browser processes.
- Response, document, DOM, screenshot, time, and whole-job budgets.
- Separation of discovery HTML from cleaned extraction HTML.
- Deterministic URL normalization, deduplication, and frontier order.
- Correct page and job outcome semantics.
- Cancellation, lease recovery, and resumability.
- API changes required to expose durable status and progress.
- Structured logs, metrics, health checks, and operational runbooks.
- Security hardening related to TLS, browser execution, and configuration.
- Test coverage and release gates for these changes.

### 2.2 Out of scope

- Internet-scale distributed crawling across regions.
- CAPTCHA solving, stealth browsing, fingerprint evasion, or anti-bot bypass.
- A general Scrapy-compatible middleware ecosystem.
- Replacing the current Markdown, PDF, EPUB, image, OCR, or metadata extractors.
- Full multi-tenant billing and quota enforcement.
- Guaranteed exactly-once network requests. Execution is at least once; persisted effects are idempotent.
- Per-subresource politeness for every third-party asset loaded by Chromium. The guaranteed politeness boundary is the top-level target origin.

---

## 3. Required system invariants

The implementation is complete only when all of the following hold:

1. Once the API returns `202 Accepted`, the crawl job and seed tasks survive API-process termination.
2. Killing a worker during a fetch does not permanently lose the task.
3. More than one worker can process the same queue without persisting duplicate page results.
4. Requests to the same origin obey one global policy across all jobs and workers.
5. A `429` or `503` response never causes immediate Playwright escalation.
6. A task cannot download or materialize more bytes than its configured limit.
7. Link discovery uses the uncleaned HTTP or rendered DOM, not the main-content extraction DOM.
8. A crawl with failed pages is not reported as fully completed.
9. The browser runs with TLS certificate verification and the Chromium sandbox enabled by default.
10. Queue depth, retries, outcomes, throttle delay, browser escalation, and resource use are measurable.
11. Crawl ordering and deduplication are reproducible for the same inputs and configuration.
12. Existing SSRF validation remains active for initial URLs, redirects, browser navigation, subresources, WebSockets, and robots.txt requests.

---

## 4. Target architecture

```text
Client
  |
  v
FastAPI API service
  |
  +---- PostgreSQL ---------------------------------------------+
  |       crawl_jobs                                             |
  |       crawl_tasks                                            |
  |       crawl_origins                                          |
  |       crawl_origin_leases                                    |
  |       crawl_page_results                                     |
  |       crawl_events                                           |
  +--------------------------------------------------------------+
          ^                    ^                       ^
          |                    |                       |
   Scheduler service     Worker processes       Reaper/finalizer
   enqueues jobs          claim leased tasks     recovers/finalizes
                                |
                                v
                     Origin policy and rate limiter
                                |
                   +------------+------------+
                   |                         |
             Shared HTTP client       Browser manager
             per worker process       one Chromium per worker
                   |                         |
                   +------------+------------+
                                |
                         Extraction pipeline
                                |
                         Artifact store
                   shared volume or S3-compatible
```

### 4.1 Process model

The production deployment MUST separate these roles:

- **API service:** validates requests, creates jobs, returns status, and never owns long-running crawl execution.
- **Worker service:** claims and executes durable page tasks.
- **Scheduler service:** converts scheduled crawl definitions into durable jobs. It MUST NOT use process-local background tasks for crawl execution.
- **Maintenance service:** reclaims expired leases, finalizes jobs, and performs retention cleanup. This MAY run inside each worker using leader election, or as a separate process.

A single-process development mode MAY combine these roles, but it MUST use the same durable repository and lease code paths.

---

# Specification ENG-001: Durable job and task execution

## 5. State model

### 5.1 Job states

| State | Terminal | Meaning |
|---|---:|---|
| `queued` | No | Job is committed but no task has started. |
| `running` | No | At least one task has started or is eligible to run. |
| `cancelling` | No | Cancellation was requested; no new work may begin. |
| `completed` | Yes | All required work is terminal and no page ended in an unexpected failure. |
| `partial` | Yes | At least one page succeeded, but one or more pages failed permanently or the job hit a budget. |
| `failed` | Yes | No page succeeded, the seed failed, or a fatal job-level error occurred. |
| `cancelled` | Yes | Cancellation completed before natural termination. |
| `timed_out` | Yes | The job deadline elapsed. |

A robots-disallowed non-seed URL is counted as `blocked`, not as an unexpected failure. A robots-disallowed seed produces a `failed` job with reason `seed_blocked_by_robots`.

### 5.2 Task states

| State | Terminal | Meaning |
|---|---:|---|
| `pending` | No | Eligible when `available_at <= now()`. |
| `leased` | No | Owned temporarily by a worker. |
| `retry_wait` | No | Retryable failure; delayed until `available_at`. |
| `succeeded` | Yes | Fetch and required extraction completed. |
| `http_error` | Yes | Non-retryable HTTP outcome. |
| `blocked_robots` | Yes | Disallowed by robots policy. |
| `extraction_failed` | Yes | Response was fetched but required extraction failed permanently. |
| `permanent_failed` | Yes | Retry budget exhausted or a non-HTTP fatal page error occurred. |
| `cancelled` | Yes | Work was cancelled before completion. |

The database MUST enforce valid transitions through repository methods. Application code MUST NOT update raw state strings ad hoc.

## 6. Data model

The following schema is normative at the logical level. Column types may be adjusted to existing repository conventions.

### 6.1 `crawl_jobs`

| Column | Requirement |
|---|---|
| `id` | UUID primary key. |
| `state` | Job-state enum; indexed. |
| `config` | Validated JSON document containing the immutable effective job configuration. |
| `max_pages` | Hard page-task ceiling for the job. |
| `max_bytes` | Whole-job downloaded-byte ceiling. |
| `discovered_count` | Number of unique page tasks accepted into the frontier. |
| `terminal_count` | Number of terminal page tasks. |
| `succeeded_count` | Number of succeeded page tasks. |
| `failed_count` | Number of unexpected terminal failures. |
| `blocked_count` | Number of robots-blocked tasks. |
| `downloaded_bytes` | Atomic whole-job committed-byte counter. |
| `reserved_bytes` | Bytes reserved by active fetches but not yet committed. |
| `browser_page_count` | Atomic count of browser-tier navigation starts. |
| `next_discovery_seq` | Monotonic sequence used for deterministic frontier order. |
| `terminal_reason` | Nullable stable reason code such as `page_budget`, `byte_budget`, `deadline`, or `seed_failed`. |
| `deadline_at` | Absolute job deadline. |
| `cancel_requested_at` | Nullable timestamp. |
| `created_at`, `started_at`, `finished_at` | Lifecycle timestamps. |
| `idempotency_key` | Nullable client key; unique within the applicable tenancy boundary. |
| `last_error_code`, `last_error_message` | Sanitized job-level failure summary. |
| `version` | Optimistic-lock integer. |

Counter columns are cached values for status reads. A reconciliation command MUST be able to recompute them from task rows.

### 6.2 `crawl_tasks`

| Column | Requirement |
|---|---|
| `id` | UUID primary key. |
| `job_id` | Foreign key to `crawl_jobs`; indexed. |
| `original_url` | First observed absolute URL. |
| `normalized_url` | Canonical frontier identity. |
| `url_hash` | SHA-256 of `normalized_url`. |
| `origin_key` | Normalized `scheme://host:effective-port`; indexed. |
| `depth` | Seed is depth `0`. |
| `discovery_seq` | Monotonic sequence assigned when first inserted. |
| `priority` | Integer; lower values execute first. |
| `discovered_from_task_id` | Nullable source-page foreign key. |
| `state` | Task-state enum. |
| `available_at` | Earliest claim time; indexed with state and priority. |
| `attempt_count` | Incremented only when a page fetch attempt begins. |
| `max_attempts` | Effective task retry limit. |
| `byte_budget_reserved` | Whole-job byte allowance reserved for the active attempt. |
| `lease_owner` | Nullable worker identifier. |
| `lease_token` | Random UUID changed on every claim. |
| `lease_expires_at` | Nullable lease deadline. |
| `http_status` | Nullable final or latest HTTP status. |
| `error_class`, `error_code`, `error_message` | Sanitized failure details. |
| `retry_after_at` | Nullable server-directed retry time. |
| `result_id` | Nullable page-result foreign key. |
| `created_at`, `updated_at`, `started_at`, `finished_at` | Lifecycle timestamps. |

Required constraints and indexes:

- Unique constraint on `(job_id, url_hash)`.
- Claim index on `(state, available_at, priority, discovery_seq)`.
- Lease-recovery index on `(state, lease_expires_at)`.
- Origin index on `(origin_key, state, available_at)`.
- All completion updates MUST match both `task_id` and the current `lease_token`; a stale worker cannot complete a task after losing its lease.

### 6.3 `crawl_origins`

This table is global across jobs so that concurrent crawls share one target-origin policy.

| Column | Requirement |
|---|---|
| `origin_key` | Primary key. |
| `robots_state` | `unknown`, `refreshing`, `ready`, `temporarily_unavailable`. |
| `robots_body` | Nullable bounded text. |
| `robots_fetched_at`, `robots_expires_at` | Cache timestamps. |
| `robots_etag`, `robots_last_modified` | Optional conditional-fetch metadata. |
| `robots_crawl_delay_ms` | Nullable parsed delay. |
| `configured_min_delay_ms` | Operator default or override. |
| `effective_max_concurrency` | Current concurrency ceiling. |
| `next_request_at` | Earliest next top-level request start. |
| `blocked_until` | Retry-After or circuit-breaker deadline. |
| `consecutive_retryable_failures` | Circuit-breaker input. |
| `circuit_state` | `closed`, `open`, `half_open`. |
| `updated_at` | Timestamp. |

### 6.4 `crawl_origin_leases`

| Column | Requirement |
|---|---|
| `origin_key` | Foreign key to `crawl_origins`. |
| `task_id` | Foreign key to `crawl_tasks`. |
| `worker_id` | Owning worker. |
| `lease_token` | Must match the task lease token. |
| `acquired_at`, `expires_at` | Permit lifetime. |

The table MUST have a primary key on `(origin_key, task_id)` and a unique constraint preventing one task from holding multiple active origin permits. Expired origin leases MUST be ignored and deleted by the reaper. The number of unexpired leases for an origin MUST never exceed `effective_max_concurrency`.

### 6.5 `crawl_page_results`

At minimum:

- `id`, `task_id`, `job_id`.
- Final URL and redirect chain.
- HTTP status, response headers allowlist, MIME type, charset.
- Fetch tier: `http`, `browser`, `document`, or `image`.
- Fetch, render, extraction, and total durations.
- Downloaded bytes and decoded bytes.
- Content hash and optional raw-response hash.
- Title, Markdown, cleaned HTML, extraction metadata, quality metadata, language, licensing fields, and change-detection fields already supported by CrawlTrove.
- Artifact URIs and SHA-256 values for screenshots, documents, images, or optional raw HTML.
- `created_at`.

`task_id` MUST be unique. A retried or duplicated worker completion MUST return the existing result rather than create another row.

### 6.6 `crawl_events`

An append-only event table SHOULD record major state changes:

- `job_created`
- `task_discovered`
- `task_leased`
- `task_retried`
- `task_succeeded`
- `task_failed`
- `job_cancel_requested`
- `job_finalized`

Events MUST contain identifiers and structured metadata, not raw response bodies. This table supports audit history and optional server-sent status updates.

## 7. Submission and idempotency

1. The API validates and resolves the effective configuration against server-side limits.
2. In one transaction, it creates the job and all seed tasks.
3. Seed URLs are normalized and deduplicated using the same algorithm as discovered URLs.
4. The transaction commits before the API returns `202`.
5. An `Idempotency-Key` header SHOULD be supported. Repeating the same key and equivalent request returns the existing job. Reusing the key with a different request returns `409 Conflict`.
6. The scheduled-crawl runner MUST call the same job-submission service rather than instantiate an in-process crawler.

## 8. Claiming and leases

### 8.1 Task claim

Workers claim tasks using `SELECT ... FOR UPDATE SKIP LOCKED` or an equivalent atomic repository operation.

A claim MUST:

- Select only `pending` or `retry_wait` tasks with `available_at <= now()`.
- Exclude jobs in terminal states or `cancelling`.
- Respect worker global capacity.
- Prefer lower `priority`, lower `depth`, and lower `discovery_seq`.
- Atomically set `state=leased`, `lease_owner`, a new `lease_token`, and `lease_expires_at`.
- Increment `attempt_count` only when the page fetch is about to begin, not when waiting for robots refresh or origin availability.
- Transition a `queued` job to `running` when its first page attempt begins.

### 8.2 Heartbeats

- Default task lease: 120 seconds.
- Default heartbeat: every 30 seconds.
- Heartbeats update the task and any active origin lease in one repository call.
- A worker MUST stop processing and discard its result if lease renewal fails or the lease token no longer matches.
- Long document extraction and browser navigation MUST continue heartbeating.

### 8.3 Lease recovery

A reaper runs at least every 30 seconds:

1. Find `leased` tasks whose lease expired.
2. Remove their origin leases.
3. If attempts remain, set `retry_wait` with crash backoff.
4. Otherwise set `permanent_failed` with code `lease_expired`.
5. Recompute or increment affected job counters.
6. Never drop an expired in-flight page from the frontier.

### 8.4 Completion transaction

Successful completion MUST be one transaction:

1. Verify task lease token.
2. Insert the page result idempotently.
3. Insert newly discovered tasks using `ON CONFLICT DO NOTHING`.
4. Update job discovery and byte counters atomically.
5. Mark the task terminal and clear its lease.
6. Release the origin lease.
7. Append events.
8. Commit.

Artifact bytes MUST be written atomically before this transaction. The database stores checksums and URIs. Orphaned artifacts are removed by a periodic garbage collector.

## 9. Cancellation and deadlines

- `POST /api/crawls/{id}/cancel` sets the job to `cancelling` and records `cancel_requested_at`.
- No new task may begin after cancellation is visible.
- Pending and retry-wait tasks are marked `cancelled`.
- Leased tasks receive a cancellation signal and get a bounded grace period.
- Browser navigation and HTTP streaming MUST be abortable.
- When no leases remain, the job becomes `cancelled`.
- At `deadline_at`, the same process runs with final state `timed_out`.
- Hitting `max_pages`, `max_bytes`, or `max_browser_pages` stops discovery and produces `partial` when more eligible work was known to exist.

## 10. Job finalization

A transactional finalizer, protected by a job-row lock or advisory lock, runs when no nonterminal task remains.

Rules:

- `completed`: one or more pages succeeded, no unexpected terminal failures, and no hard budget truncated known work.
- `partial`: at least one page succeeded and at least one unexpected task failed, or a hard budget truncated work.
- `failed`: zero pages succeeded, the seed failed, or a fatal job error occurred.
- `cancelled`: cancellation requested and all tasks terminal.
- `timed_out`: deadline exceeded.
- Blocked non-seed pages are reported in counts but do not alone force `partial`.
- A blocked, failed, or invalid seed prevents a `completed` result.

---

# Specification ENG-002: Host-aware politeness, robots, retries, and browser escalation

## 11. Origin identity

The origin key MUST be:

```text
lowercase-scheme://idna-lowercase-host:effective-port
```

Examples:

- `https://example.com:443`
- `http://example.com:80`

Default ports are explicit in the origin key. Scope comparison MUST use this normalized origin, not raw `netloc`.

## 12. Global origin policy

The origin scheduler applies across every worker and job.

Defaults:

- Per-origin concurrency: `1`.
- Minimum delay between top-level request starts: `1000 ms`.
- Maximum retry attempts: `3`.
- Maximum redirects: `10`.
- Circuit opens after `5` consecutive retryable failures.
- Circuit open interval: `300 seconds`.
- Half-open probes: `1`.

Job requests MAY ask for stricter settings. They MUST NOT exceed operator-defined concurrency or reduce operator-defined delay.

Before every top-level HTTP request or browser navigation, the worker MUST atomically acquire an origin permit. Permit acquisition enforces:

1. Unexpired active permits are below the concurrency limit.
2. `now() >= next_request_at`.
3. `now() >= blocked_until`.
4. Circuit state permits a request.
5. Robots policy is available and permits the URL.

After acquisition, `next_request_at` is advanced by the effective delay. The effective delay is the maximum of operator minimum, job minimum, and parsed robots crawl delay.

A worker MUST NOT occupy an execution slot while sleeping through a material origin delay. When a permit cannot be acquired promptly, the worker updates the task's `available_at` to the origin's next eligible time, releases the task lease without incrementing `attempt_count`, and claims other work. Claim queries SHOULD incorporate origin eligibility to reduce churn.

Redirect destinations MUST be revalidated and scheduled against their own normalized origin policy before following. Browser top-level redirect requests MUST pass through the same permit and robots checks; ordinary subresources remain outside the guaranteed politeness boundary.

## 13. Robots.txt

### 13.1 Defaults

- `respect_robots` is `true`.
- Public API clients cannot disable it unless the operator sets `ALLOW_ROBOTS_OVERRIDE=true` and the caller has a privileged authorization scope.
- Product token: `CrawlTrove`.
- Cache TTL: 24 hours, bounded by a configurable minimum and maximum.
- Robots body limit: 1 MiB.
- Robots fetches use the same SSRF, redirect, TLS, timeout, response-size, and origin-delay controls as page fetches.

### 13.2 Response policy

| Robots outcome | Required behavior |
|---|---|
| `2xx` | Parse rules and cache. |
| `304` | Refresh cache expiry. |
| `404` or `410` | Cache `allow_all`. |
| `401` or `403` | Cache `disallow_all`. |
| `429` | Respect `Retry-After`; defer origin. |
| `5xx`, timeout, DNS, or TLS error | Retry with backoff; do not crawl pages until policy is resolved under the default `retry` failure policy. |

Supported directives:

- `User-agent`
- `Allow`
- `Disallow`
- `Sitemap`
- `Crawl-delay` when present

Robots-provided sitemaps SHOULD be added as seed sources subject to the same scope and budget rules.

### 13.3 Failure policy

Operator option `ROBOTS_FAILURE_POLICY`:

- `retry` — default; defer the origin.
- `deny` — fail closed after retry exhaustion.
- `allow` — permit crawling after retry exhaustion; intended only for controlled internal environments.

The effective policy MUST be recorded in job configuration and page metadata.

## 14. Retry matrix

| Condition | Retry | Browser fallback |
|---|---:|---:|
| DNS, connect, TLS, read timeout | Yes | No |
| HTTP `408`, `425`, `429`, `500`, `502`, `503`, `504` | Yes | No |
| HTTP `401`, `403`, `404`, `410` | No by default | No |
| Other `4xx` | No by default | No |
| Valid `2xx` HTML with detected client-rendered shell or extraction below threshold | No network retry | At most once |
| Browser process crash | Yes, after browser restart | Already browser tier |
| Deterministic parser error on valid body | No after one diagnostic retry | No |
| Response exceeds size limit | No | No |

`Retry-After` MUST be parsed as seconds or an HTTP date. The crawler MUST NOT shorten a server-specified delay. When the delay exceeds the job deadline or the operator's maximum queued-wait duration, the task becomes terminal with a stable deferment reason rather than sending an earlier request. When `Retry-After` is absent, use full-jitter exponential backoff:

```text
cap = min(retry_max, retry_base * 2^(attempt - 1))
delay = random_uniform(0, cap)
```

Defaults:

- `retry_base = 1 second`
- `retry_max = 60 seconds`
- Maximum queued wait without ending the task = 1 hour; longer server delays are still honored by not retrying within the current job

## 15. Circuit breaker

The origin circuit opens when any configured threshold is met, including:

- Five consecutive retryable failures.
- Three `429` responses within two minutes.
- A server `Retry-After` deadline.

While open, eligible tasks move to `retry_wait` until `blocked_until`; they are not browser-escalated. After the deadline, one half-open probe is allowed. Success closes the circuit and resets counters. Failure reopens it.

## 16. Browser escalation policy

`browser_mode` values:

- `never`
- `auto` — default
- `always`

In `auto`, browser escalation is permitted only when:

1. The HTTP response is a successful HTML response.
2. The body is within limits.
3. The extractor detects a client-rendered shell, a known hydration pattern, or insufficient main content despite substantial script/application markup.
4. The page has not already used the browser tier.

Browser escalation MUST NOT occur because of:

- `429`
- `503`
- DNS or connection failure
- TLS failure
- robots denial
- response-size failure
- unsupported MIME type

The escalation reason MUST be stored and emitted as a metric.

---

# Specification ENG-003: Resource-bounded fetch runtime

## 17. Shared HTTP client

Each worker process MUST create one shared asynchronous HTTP client during startup and close it during shutdown.

Required behavior:

- Connection and TLS-session reuse.
- Configurable global and per-host connection limits.
- Environment proxy inheritance disabled by default.
- Existing DNS pinning, redirect revalidation, and connected-IP verification retained.
- Streaming response reads.
- Abort before materializing a body beyond its limit.
- Separate connect, response-header, idle-read, and total deadlines.
- Bounded decompressed bytes as well as transferred bytes.
- No simultaneous materialization of both full text and full byte copies unless an extractor explicitly requires it.

Suggested defaults:

| Limit | Default |
|---|---:|
| Global HTTP connections per worker | 100 |
| Per-host HTTP connections per worker | 10 |
| Connect timeout | 10 s |
| Header timeout | 20 s |
| Total HTTP page timeout | 60 s |
| HTML decoded body | 10 MiB |
| PDF/EPUB body | 50 MiB |
| Image body | 25 MiB |
| Redirect count | 10 |

Server-side hard caps MUST bound every user-configurable value.

## 18. Browser manager

Each worker process MUST maintain one long-lived Playwright runtime and one long-lived Chromium process.

Required behavior:

- A bounded semaphore controls concurrent browser contexts.
- Default concurrent contexts per worker: `2`.
- Each task receives a fresh incognito context to isolate cookies and storage.
- Context and page are closed after each task; the Chromium process is reused.
- Browser launch count is one per worker process plus crash recovery.
- TLS certificate verification is enabled by default.
- Chromium sandbox remains enabled by default.
- Service workers remain blocked unless a future feature explicitly requires them.
- Browser version and declared user-agent MUST be consistent.
- Browser crashes trigger one controlled restart and retry.
- A worker stops claiming browser tasks while Chromium is restarting.
- Browser-process RSS and context utilization are exported as metrics.

Suggested defaults:

| Limit | Default |
|---|---:|
| Navigation timeout | 60 s |
| Total browser task timeout | 90 s |
| Maximum DOM serialization | 10 MiB UTF-8 |
| Maximum screenshot | 20 MiB |
| Maximum browser pages per job | 100 or operator-defined percentage of `max_pages` |
| Browser contexts per worker | 2 |

## 19. Screenshot behavior

- `capture_screenshot` defaults to `false`.
- No screenshot API call may occur when the option is false.
- When true, format, quality, full-page behavior, and maximum dimensions are explicit configuration.
- Screenshots exceeding byte or dimension limits fail as an optional artifact; they MUST NOT fail otherwise successful text extraction unless the request marked screenshots as required.
- Screenshot failure is recorded separately.

## 20. Whole-job budgets

Every job configuration MUST contain operator-bounded limits:

- Maximum unique pages.
- Maximum crawl depth.
- Maximum downloaded bytes.
- Maximum browser navigations.
- Maximum wall-clock duration.
- Maximum artifacts bytes.
- Maximum unique origins when cross-origin crawling is enabled.
- Maximum failures before optional early termination.

Before reading a response body, a worker MUST atomically reserve a byte allowance from:

```text
max_bytes - downloaded_bytes - reserved_bytes
```

The reservation is bounded by the task's response-size cap and is stored on the task. The stream cannot read beyond that allowance. On completion or failure, the worker converts actual transferred/decoded usage into `downloaded_bytes` and releases the unused reservation. Lease recovery also releases abandoned reservations. This prevents concurrent workers from materially exceeding the whole-job byte budget.

Browser navigation count is reserved with an atomic conditional update before launching the browser tier. Discovery stops when the page budget is exhausted. The terminal reason is exposed in job status.

## 21. Memory pressure

A worker SHOULD detect its cgroup or process memory ceiling.

- At 80% utilization, stop claiming new tasks.
- At 90%, close idle browser contexts and restart Chromium if browser memory is dominant.
- If pressure persists, release unstarted leases and fail readiness until memory falls below the resume threshold.
- A task killed by the operating system is recovered through lease expiry.

---

# Specification ENG-004: Discovery, URL identity, and crawl-trap controls

## 22. Separate page representations

The scrape result contract MUST separate:

- `discovery_html`: uncleaned HTTP HTML or rendered DOM used for links and document metadata.
- `content_html`: cleaned main-content HTML used for Markdown extraction.
- `markdown`: final extracted text.
- `raw_body_ref`: optional artifact reference; disabled by default.

Link extraction, canonical parsing, meta-robots evaluation, pagination discovery, and media discovery MUST use `discovery_html`.

The main-content cleaner MUST use CSS-selector APIs for selectors such as `.header`, `#footer`, and `.nav`; tag-name search APIs are not valid substitutes.

## 23. Link extraction

The discovery parser MUST process links in document order from:

- `<a href>`
- `<area href>`
- `<link rel="next|prev|canonical|alternate">`
- `<iframe src>` only when explicitly enabled
- Embedded document/media links when their feature is enabled
- Sitemap entries

Required processing order:

1. Apply `<base href>` when valid.
2. Resolve to an absolute URL.
3. Remove the fragment.
4. Validate scheme and URL safety.
5. Normalize the URL.
6. Evaluate scope and filters.
7. Apply meta-robots and `rel=nofollow` policy.
8. Insert idempotently into the frontier.

Use ordered deduplication. `set()` conversion MUST NOT determine traversal order.

## 24. URL normalization

The default normalization algorithm MUST:

- Lowercase scheme.
- Convert host to lowercase IDNA form.
- Remove user-info.
- Remove fragments.
- Remove default ports from the serialized URL while retaining effective ports in `origin_key`.
- Resolve dot segments in paths.
- Preserve path case.
- Convert an empty path to `/`.
- Normalize percent encoding without decoding reserved delimiters.
- Preserve query parameter order and duplicates by default.
- Remove only operator-configured tracking parameters.
- Reject URLs longer than the configured maximum.
- Produce a deterministic UTF-8 serialization before hashing.

Do not sort all query parameters by default because order and duplication can be semantically significant.

Canonical-link handling:

- Record the canonical URL as metadata.
- Use it for deduplication only when it passes URL safety, lies within configured scope, and does not create a canonical cycle.
- Cross-origin canonical URLs are recorded but are not followed automatically unless cross-origin scope permits them.

## 25. Frontier behavior

- Maintain distinct pending, leased, terminal, and failed states in PostgreSQL.
- The unique `(job_id, url_hash)` constraint is the final deduplication guard.
- The first observed URL and discovery source are retained.
- Frontier order is deterministic by priority, depth, then discovery sequence.
- Seed URLs have highest priority.
- Sitemap URLs SHOULD be lower priority than explicit seeds and higher priority than ordinary deep links.
- Retry tasks retain their original discovery sequence.
- The page limit is enforced atomically during insertion so concurrent workers cannot exceed it materially.

## 26. Scope modes

Supported modes:

- `same_origin` — default.
- `same_site` — registrable-domain scope, using a maintained public-suffix implementation.
- `allowlist` — explicit origins or domains.
- `any_public` — operator-disabled by default.

Scope checks use normalized host and effective port. Redirects outside scope are not followed unless `follow_external_redirects` is explicitly enabled.

## 27. Documents and images

Options:

- `include_documents`: default `true` for PDF and EPUB.
- `include_images`: default `false`.
- `discover_embedded_images`: default `false`.
- `extract_linked_images`: default `false`.

Ordinary link discovery MUST NOT discard PDF, EPUB, or supported image URLs when their corresponding option is enabled.

## 28. Crawl-trap controls

Defaults:

| Control | Default |
|---|---:|
| Maximum URL length | 4096 bytes |
| Maximum path segments | 50 |
| Maximum query variants per normalized path | 20 |
| Maximum repeated identical path segment | 5 |
| Maximum children accepted from one page | 1000 |
| Maximum redirect hops | 10 |
| Maximum unique origins per same-origin job | 1 |

The crawler SHOULD detect common calendar, faceted-search, session-ID, and infinite-pagination patterns. Trap rejections are stored as counters with a reason but do not require task rows.

---

# Specification ENG-005: Result semantics and API contract

## 29. Page success criteria

A normal HTML page is `succeeded` only when:

1. The final response status is accepted by policy, normally `2xx`.
2. The response is within limits.
3. Required extraction completes.
4. The result transaction commits.

A content-rich `404` remains `http_error`; extracted body text MAY be retained as diagnostic metadata but MUST NOT be reported as a successful page.

A `204` may be `succeeded` with `empty=true` when no content was requested. A `200` page with no extractable content is either:

- `succeeded` with `empty=true` when policy allows empty pages, or
- `extraction_failed` with `no_extractable_content`.

The selected policy is explicit in job configuration.

## 30. Proposed API

The current crawl-start route SHOULD remain as a compatibility alias. New behavior is defined under `/api/crawls`.

### 30.1 Create crawl

`POST /api/crawls`

```json
{
  "seed_urls": ["https://example.com/"],
  "max_pages": 1000,
  "max_depth": 5,
  "scope": "same_origin",
  "browser_mode": "auto",
  "respect_robots": true,
  "per_origin_concurrency": 1,
  "min_delay_ms": 1000,
  "max_retries": 3,
  "capture_screenshot": false,
  "include_documents": true,
  "include_images": false,
  "job_timeout_seconds": 21600
}
```

Response: `202 Accepted`

```json
{
  "job_id": "uuid",
  "state": "queued",
  "created_at": "RFC3339 timestamp",
  "status_url": "/api/crawls/{job_id}"
}
```

### 30.2 Read crawl

`GET /api/crawls/{job_id}`

```json
{
  "job_id": "uuid",
  "state": "running",
  "terminal_reason": null,
  "counts": {
    "discovered": 420,
    "pending": 120,
    "leased": 8,
    "retry_wait": 4,
    "succeeded": 275,
    "http_error": 5,
    "blocked_robots": 3,
    "extraction_failed": 2,
    "permanent_failed": 3,
    "cancelled": 0
  },
  "resources": {
    "downloaded_bytes": 123456789,
    "browser_pages": 18,
    "elapsed_seconds": 640
  },
  "created_at": "RFC3339 timestamp",
  "started_at": "RFC3339 timestamp",
  "finished_at": null
}
```

The API MUST not expose internal lease tokens, raw database errors, or stack traces.

### 30.3 List pages

`GET /api/crawls/{job_id}/pages?state=succeeded&cursor=...&limit=100`

Returns cursor-paginated page summaries in deterministic discovery order.

### 30.4 Cancel crawl

`POST /api/crawls/{job_id}/cancel`

- `202` when cancellation begins.
- `200` when already cancelled.
- `409` when the job is already terminal in another state.

### 30.5 Retry failed pages

P1 endpoint:

`POST /api/crawls/{job_id}/retry-failed`

Creates a new job referencing selected terminal failures. It MUST NOT mutate the historical result of the original job.

## 31. Compatibility mapping

For existing clients:

- Legacy `status=completed` maps only to the new `completed`.
- `partial`, `failed`, `cancelled`, and `timed_out` remain distinct; they MUST not be collapsed into `completed`.
- Existing result fields remain available for at least one deprecation cycle.
- Existing page and batch limits may be retained as server defaults, but the durable backend must not depend on those small values for correctness.

---

# Specification ENG-006: Observability and operations

## 32. Metrics

Expose Prometheus-compatible metrics. Do not use raw URLs, job IDs, or origin names as metric labels.

Required metrics use stable, low-cardinality enum labels only. Dynamic exception names, URLs, origins, and job identifiers are forbidden as labels.

| Metric | Type |
|---|---|
| `crawltrove_jobs_total{state}` | Counter |
| `crawltrove_jobs_active` | Gauge |
| `crawltrove_tasks_total{outcome,error_class}` | Counter |
| `crawltrove_queue_depth{state}` | Gauge |
| `crawltrove_task_claim_seconds` | Histogram |
| `crawltrove_task_duration_seconds{tier}` | Histogram |
| `crawltrove_fetch_duration_seconds{tier}` | Histogram |
| `crawltrove_extract_duration_seconds{type}` | Histogram |
| `crawltrove_download_bytes_total{mime_group}` | Counter |
| `crawltrove_retry_total{reason}` | Counter |
| `crawltrove_origin_throttle_wait_seconds` | Histogram |
| `crawltrove_robots_decisions_total{decision}` | Counter |
| `crawltrove_browser_escalations_total{reason}` | Counter |
| `crawltrove_browser_contexts_active` | Gauge |
| `crawltrove_browser_restarts_total{reason}` | Counter |
| `crawltrove_lease_expirations_total` | Counter |
| `crawltrove_worker_memory_bytes` | Gauge |
| `crawltrove_artifact_write_failures_total{type}` | Counter |

## 33. Structured logs

Every task lifecycle log includes:

- `timestamp`
- `level`
- `service`
- `worker_id`
- `job_id`
- `task_id`
- `origin_key`
- `attempt`
- `state`
- `event`
- `duration_ms` where relevant
- `http_status` where relevant
- `error_class` and stable `error_code`

Raw response bodies, authorization headers, cookies, API keys, and full query strings MUST NOT be logged by default.

## 34. Health endpoints

- `/health/live`: process event loop is responsive.
- `/health/ready`: database reachable, migrations compatible, artifact store writable, and worker browser available when browser execution is enabled.
- `/metrics`: protected or bound to an internal interface in production.

A worker that cannot renew leases MUST fail readiness and stop claiming tasks.

## 35. Operational commands

Provide commands or admin jobs for:

- Reconcile job counters.
- Requeue expired leases.
- Inspect dead-letter/permanent failures.
- Rebuild robots cache for one origin.
- Cancel a stuck job.
- Validate artifact references and delete orphans.
- Purge jobs and artifacts according to retention policy.
- Report schema and worker-version compatibility.

---

# Specification ENG-007: Security and configuration hardening

## 36. Required security behavior

1. Preserve all current URL-safety and SSRF checks.
2. Revalidate every redirect destination and actual connected address.
3. Apply URL safety to robots requests and browser subresources.
4. Keep environment proxy inheritance disabled unless explicitly configured.
5. Enable browser TLS verification by default.
6. Keep the Chromium sandbox enabled by default.
7. Treat disabling TLS verification, robots enforcement, or the sandbox as privileged operator configuration, never ordinary job input.
8. Restrict user-supplied request headers. At minimum reject or control `Host`, `Connection`, `Content-Length`, `Transfer-Encoding`, `Proxy-*`, `Cookie`, and hop-by-hop headers.
9. Redact secrets from logs and page metadata.
10. Use a shared persistent volume or S3-compatible store before enabling workers on different hosts.
11. Use database roles with least privilege; API and workers SHOULD use separate credentials.
12. Apply outbound firewall controls in production as defense in depth.

## 37. Configuration

Suggested environment variables and defaults:

| Variable | Default |
|---|---:|
| `CRAWLER_EXECUTION_BACKEND` | `durable` after rollout |
| `CRAWLER_WORKER_MAX_INFLIGHT` | `20` |
| `CRAWLER_TASK_LEASE_SECONDS` | `120` |
| `CRAWLER_HEARTBEAT_SECONDS` | `30` |
| `CRAWLER_PER_ORIGIN_CONCURRENCY` | `1` |
| `CRAWLER_MIN_DELAY_MS` | `1000` |
| `CRAWLER_MAX_RETRIES` | `3` |
| `CRAWLER_RETRY_BASE_MS` | `1000` |
| `CRAWLER_RETRY_MAX_MS` | `60000` |
| `CRAWLER_CIRCUIT_FAILURES` | `5` |
| `CRAWLER_CIRCUIT_OPEN_SECONDS` | `300` |
| `CRAWLER_ROBOTS_TTL_SECONDS` | `86400` |
| `ROBOTS_FAILURE_POLICY` | `retry` |
| `ALLOW_ROBOTS_OVERRIDE` | `false` |
| `CRAWLER_HTTP_MAX_CONNECTIONS` | `100` |
| `CRAWLER_HTTP_MAX_PER_HOST` | `10` |
| `CRAWLER_BROWSER_CONTEXTS` | `2` |
| `CRAWLER_MAX_HTML_BYTES` | `10485760` |
| `CRAWLER_MAX_DOCUMENT_BYTES` | `52428800` |
| `CRAWLER_MAX_IMAGE_BYTES` | `26214400` |
| `CRAWLER_MAX_URL_BYTES` | `4096` |
| `CRAWLER_MAX_REDIRECTS` | `10` |
| `CRAWLER_DEFAULT_JOB_TIMEOUT_SECONDS` | `21600` |
| `BROWSER_IGNORE_HTTPS_ERRORS` | `false` |
| `BROWSER_DISABLE_SANDBOX` | `false` |
| `ARTIFACT_STORE_BACKEND` | `filesystem` |

Invalid combinations MUST fail startup rather than silently downgrade safety.

---

# Specification ENG-008: Artifact storage

## 38. Storage interface

Define an `ArtifactStore` interface:

- `put(stream, media_type, expected_max_bytes) -> ArtifactRef`
- `get(ref) -> stream`
- `delete(ref)`
- `exists(ref)`
- `verify(ref, sha256, size)`

Implementations:

- `FilesystemArtifactStore` for a shared persistent volume.
- `S3ArtifactStore` as P1 for multi-host deployments.

Artifact paths or keys MUST be derived from immutable identifiers and content hashes, not unsanitized URLs. Writes use a temporary object followed by atomic publish. Database rows store URI, byte size, media type, and SHA-256.

Raw HTML retention is off by default. Retention policy is independently configurable for:

- Markdown and metadata.
- Screenshots.
- Original documents and images.
- Diagnostic raw bodies.
- Failed-job artifacts.

---

# Specification ENG-009: Testing and release gates

## 39. Unit tests

Required unit coverage:

- URL normalization, IDNA, default ports, fragments, percent encoding, and tracking-parameter removal.
- Same-origin and same-site scope decisions.
- Deterministic link order.
- CSS-selector removal in the content cleaner.
- Retry classification and backoff bounds.
- `Retry-After` parsing.
- Circuit-breaker transitions.
- Robots rule selection, allow/disallow precedence, and cache behavior.
- Job final-state calculation.
- State-transition validation.
- Response-size enforcement before full materialization.
- Screenshot disabled path makes no screenshot call.
- Browser-escalation decision matrix.
- Error and log redaction.

## 40. Integration tests

Use PostgreSQL and a controllable local HTTP fixture service.

Required cases:

1. Ten workers claim 10,000 tasks with no simultaneous duplicate lease.
2. Two workers attempt completion for the same task; one persisted result exists.
3. A worker is killed after fetch and before completion; the task is recovered after lease expiry.
4. A stale worker with an old lease token cannot commit.
5. Multiple jobs targeting one origin obey one shared concurrency and delay policy.
6. A `429 Retry-After: 30` causes no request to that origin for at least 30 seconds and no browser launch.
7. A `503` retries with bounded jitter.
8. A redirect to a private or loopback address is rejected.
9. Robots disallow prevents page fetch.
10. A navigation menu outside `<main>` still supplies crawl links.
11. Linked PDFs are discovered when documents are enabled.
12. Duplicate pending URLs are rejected by the database constraint.
13. A content-rich `404` is not a successful page.
14. Job state is `partial` when one page succeeds and another exhausts retries.
15. Cancellation prevents new claims and terminates in-flight work within the grace period.
16. Browser process count remains bounded while many browser tasks execute.
17. TLS-invalid browser target fails by default.
18. Whole-job byte and page budgets stop additional work.
19. Scheduled crawls remain executable after scheduler-process restart.
20. Two API processes return the same durable job status.

## 41. Fault-injection test

Run a sustained fixture crawl while terminating random workers.

Acceptance criteria:

- No accepted task is lost.
- Every task eventually becomes terminal after workers stabilize.
- Persisted page results remain unique.
- Duplicate network requests caused by at-least-once recovery are measured and remain within the configured lease/timeout behavior.
- Job counters reconcile exactly with task rows.
- No origin exceeds configured concurrent top-level requests.
- Minimum request-start delay is respected within a documented scheduler tolerance.

## 42. Load and resource tests

Minimum release test:

- 100,000 queued page tasks.
- At least eight worker processes.
- Mixed HTTP, retry, document, and browser fixture routes.
- Queue claim latency, database CPU, worker RSS, browser RSS, and completion rate recorded.

Release acceptance:

- Queue operations remain stable without deadlocks.
- One Chromium process per worker, excluding controlled restarts.
- No response exceeds configured decoded-byte limits.
- Worker memory remains below the configured pause threshold during steady state.
- Metrics cardinality remains bounded.
- A complete crawl result can be reconstructed after all API and worker processes restart.

## 43. Static and supply-chain gates

Add the following CI gates:

- Ruff formatting and linting.
- Pyright or mypy.
- Bandit or equivalent security static analysis.
- `pip-audit`.
- CodeQL or equivalent code scanning.
- Reproducible Python dependency lock.
- SBOM generation for release images.
- Container vulnerability scan.
- Secret scanning.
- Migration forward and rollback validation.
- Signed release images where the publishing environment supports it.

A release MUST fail on migration errors, high-severity dependency vulnerabilities without an approved exception, or failed durability/politeness integration tests.

---

# 44. Implementation work packages

## P0 — required before production scaling

### WP-001: Durable repository and migrations

Deliverables:

- Job, task, origin, origin-lease, and result schema.
- State-transition repository.
- Atomic submission and idempotency.
- Claim, heartbeat, completion, retry, and reaper operations.
- Counter reconciliation command.

Acceptance:

- Multi-process integration tests pass.
- API process termination after `202` does not affect execution.

### WP-002: Worker runtime

Deliverables:

- Standalone worker entry point.
- Configurable capacity.
- Lease heartbeats.
- Graceful shutdown and cancellation.
- Scheduler updated to enqueue jobs rather than spawn local crawl tasks.

Acceptance:

- Two or more workers can process one job.
- Random worker termination does not lose pages.

### WP-003: Origin scheduler and robots

Deliverables:

- Global per-origin concurrency and delay.
- Robots cache and parser.
- Retry-After, exponential backoff, and circuit breaker.
- Browser-escalation decision changes.

Acceptance:

- `429` and robots integration tests pass.
- Concurrent jobs share one origin policy.

### WP-004: HTTP and browser resource managers

Deliverables:

- Shared HTTP client.
- Streaming byte limits.
- Long-lived Chromium per worker.
- Fresh bounded contexts.
- Conditional screenshots.
- TLS verification enabled.

Acceptance:

- Browser launch count is bounded.
- Oversized responses terminate before full materialization.

### WP-005: Discovery separation and deterministic frontier

Deliverables:

- `discovery_html` and `content_html` split.
- Ordered link extraction from raw/rendered DOM.
- Correct CSS-selector cleaning.
- URL normalization, scope, document discovery, and trap controls.
- Database-enforced pending deduplication.

Acceptance:

- Navigation links outside main content are crawled.
- Repeated runs produce the same frontier order for the same fixture.

### WP-006: Outcome semantics and API

Deliverables:

- New page/job states.
- Durable status endpoint and page pagination.
- Cancellation.
- Compatibility mapping.
- Correct non-2xx handling.

Acceptance:

- Partial and failed jobs are never reported as completed.
- Status is consistent across multiple API processes.

## P1 — required for mature operations

### WP-007: Observability and operations

Deliverables:

- Metrics, structured logs, readiness, admin reconciliation, retention, and artifact garbage collection.

### WP-008: S3-compatible artifact store

Deliverables:

- Shared object storage implementation, checksum verification, retention, and orphan cleanup.

### WP-009: Retry-failed workflow and event streaming

Deliverables:

- New-job retry of selected failed pages.
- Optional server-sent events backed by `crawl_events`.

### WP-010: Release hardening

Deliverables:

- Dependency locking, static analysis, vulnerability scanning, SBOM, and image signing.

---

# 45. Rollout plan

1. **Additive schema deployment**
   - Deploy new tables, enums, indexes, and repository code.
   - Keep legacy execution selectable through `CRAWLER_EXECUTION_BACKEND=legacy|durable`.

2. **Durable worker deployment**
   - Deploy one worker with durable execution disabled for public traffic.
   - Run integration and shadow fixture jobs.

3. **Drain legacy jobs**
   - Stop accepting legacy jobs.
   - Allow active process-local jobs to finish or explicitly mark them interrupted. Their in-memory frontier cannot be migrated safely.

4. **Enable durable submission**
   - Set the API and scheduler to create durable jobs.
   - Start with one worker and validate counters, artifacts, and metrics.

5. **Enable multiple workers**
   - Add workers gradually.
   - Validate lease recovery, origin policy, database contention, and browser limits.

6. **Enable new terminal states**
   - Update UI and API clients before removing legacy status compatibility.

7. **Remove legacy executor**
   - Remove process-local `jobs` state and FastAPI background-task execution after one deprecation cycle.

Rollback is performed by stopping durable workers and switching new submissions back to legacy only while the compatibility path exists. Additive database objects remain in place. Do not downgrade after irreversible migrations without a tested rollback migration.

---

# 46. Definition of done

The program is complete when:

- All P0 work packages are merged.
- Required migrations and rollback paths are tested.
- Durability, politeness, discovery, failure-semantics, and resource-limit integration tests pass.
- The API and worker run as separate services in the standard Compose deployment.
- Multiple API processes and multiple workers return consistent job state.
- Existing SSRF tests continue to pass.
- Production defaults enforce robots, TLS verification, browser sandboxing, per-origin delay, and resource limits.
- The dashboard distinguishes `completed`, `partial`, `failed`, `cancelled`, and `timed_out`.
- Operational metrics and runbooks exist for stuck leases, queue backlog, high retry rates, browser crashes, and artifact failures.
- A fault-injection crawl completes without lost tasks or duplicate persisted results.
