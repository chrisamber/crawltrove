# CrawlTrove v0.4.0

Version 0.4 makes durable PostgreSQL crawling the supported multi-worker core,
adds dedicated managed-acquisition integrations, and introduces bounded human
intervention sessions plus operator health, metrics, and maintenance surfaces.
The single-page `/api/scrape` contract remains compatible and can still run
without PostgreSQL; durable `/api/crawl` requires PostgreSQL.

## Migration

- Back up PostgreSQL and artifact storage before upgrading.
- Startup applies the forward-only, additive migrations through
  `0012_queue_claim_performance.sql`. The compatibility gate upgrades a v0.3
  fixture and proves existing `scrape_runs` and `scraped_pages` remain intact.
- Completed legacy scrape data remains readable. In-flight crawls from the
  pre-durable implementation are not automatically imported into `crawl_jobs`.
- The new queue index keeps ordered `FOR UPDATE SKIP LOCKED` claims bounded as
  terminal rows accumulate.
- Keep the backup until `/health/ready`, the dashboard, and representative
  searches succeed. Use `python -m app.admin compatibility` to apply/report
  pending migrations explicitly.

## Remote workers

The supported topology is one core service plus acquisition workers that
connect directly to PostgreSQL. Workers receive narrowly scoped enrollment
bundles, advertise protocol version and capabilities, heartbeat through their
database identity, and write immutable artifacts to a worker-scoped S3 prefix.

- Set `CRAWLTROVE_REMOTE_WORKERS=true`, `CRAWLTROVE_WORKER_ID=core`, and use
  `ARTIFACT_STORE_BACKEND=s3` on the core. Filesystem artifacts are
  intentionally rejected in remote mode.
- Enroll each worker with `scripts/enroll_worker.py`; distribute its mode-0600
  bundle out of band. Production database connections require verified TLS and
  client credentials. `WORKER_ALLOW_INSECURE_DB=true` is local-only.
- Workers with an incompatible protocol remain visible but cannot claim work,
  allowing an operator to drain and replace them safely.
- Standard, browser, and CAPTCHA workers use dedicated enrollment bundles and
  artifact prefixes. The default Compose stack auto-enrolls only local workers.

## Provider budgets

Managed acquisition is disabled unless its dedicated credentials are present.
The router reserves and commits provider-native usage atomically with task
state; it does not convert providers to an invented common credit.

| Provider | Credentials | Native meters |
| --- | --- | --- |
| Firecrawl | `FIRECRAWL_API_KEY` | `credits` |
| Bright Data Web Unlocker | `BRIGHTDATA_API_KEY`, `BRIGHTDATA_UNLOCKER_ZONE` | `requests` |
| Browserbase | `BROWSERBASE_API_KEY`, `BROWSERBASE_PROJECT_ID` | `browserMinutes`, `proxyBytes` |

Per-job `acquisition.creditBudgets` limits those native meters. A requested but
unconfigured provider fails as unavailable; it is never silently treated as a
successful local acquisition. The private release smoke is deliberately
sequential and capped:

```bash
.venv/bin/python scripts/smoke_providers.py --preflight
.venv/bin/python scripts/smoke_providers.py
```

## Human intervention

Browser workers can park a fenced task in a durable live session, issue
single-use scoped tokens, and resume the same browser context without spending
the task lease twice. Sessions default to 15 minutes and accept a bounded
5-to-60-minute TTL. Navigation, actions, screenshots, DOM transfer, and
artifacts are size- and time-bounded; expired or cancelled sessions terminalize
their task and release reservations.

Persisted browser profiles are encrypted with `SESSION_ENCRYPTION_KEY`.
`SESSION_ENCRYPTION_PREVIOUS_KEYS` supports read-time key rotation. First-party
CAPTCHA automation is opt-in for exact authorized domains and image/text
challenges; token challenges continue to require a human or approved managed
provider.

## Operations

- `/health/live` is process liveness. `/health/ready` requires PostgreSQL,
  compatible migrations, artifact storage, lease maintenance, and any required
  browser capacity.
- `/metrics` exposes bounded-label Prometheus series. It follows application
  authentication unless `METRICS_BIND_INTERNAL=true` and
  `PUBLISHED_BIND_ADDRESS` is explicitly loopback.
- Authenticated `/api/operations/*` reads expose workers, providers, sessions,
  attempts, failures, and job reconciliation without returning credentials or
  capture bodies.
- `python -m app.admin` provides `reconcile-job`, `reap-leases`,
  `list-failures`, `validate-artifacts`, `cleanup-temporary`, `compatibility`,
  and exactly confirmed `purge-job` commands.
- `DATA_RETENTION_DAYS` and `DATA_KEEP_RUNS` retain the existing scheduled
  artifact-pruning behavior. Destructive job purge requires the identical job
  UUID in both the target and `--confirm` argument.

## Evaluation

The acquisition evaluation compares CrawlTrove, Firecrawl's direct `/v2/scrape`
API, and pinned Crawl4AI 0.9.2 on the same public fixtures. Install
`requirements-eval.txt` and Crawl4AI's Chromium runtime, start CrawlTrove, then
run the paid-call preflight before the five-run report:

```bash
FIRECRAWL_API_KEY=... .venv/bin/python -m eval.acquisition --dry-run
FIRECRAWL_API_KEY=... .venv/bin/python -m eval.acquisition
```

Reports are written to ignored `tmp/acquisition-eval-<UTC>.json` files with
fixture hashes, correctness, latency range/median, output size, and native
usage; bodies and secrets are omitted. This benchmark is report-only, not a CI
gate. No paid v0.4 result is claimed without the generated report. Historical
results remain in [the frontier evaluation](frontier-scraping-evaluation.md).

The release candidate passed 760 Python tests, 14 dashboard tests, Ruff, scoped
Mypy, high-severity Bandit, a fresh runtime dependency audit, dashboard type and
production builds, Compose build/start/readiness, and a disposable PostgreSQL
load of 100,000 tasks with eight workers. The load completed in 365.781 seconds
at 273.39 tasks/second, including 25,000 retries, with p95 claim latency
11.826 ms, 47,857,664-byte maximum worker RSS, no duplicate origin leases, and
intact byte and artifact caps.

## Known limitations

- Browserbase usage supports its native session accounting but does not accept
  an arbitrary CrawlTrove-owned proxy endpoint; use a provider-supported proxy
  configuration or an owned browser worker when egress identity must be fixed.
- Remote production workers require external TLS/S3 enrollment secret
  distribution; Compose's insecure database override is not a production
  deployment recipe.
- The 100,000-task load is a durable queue/capability stress test. Its browser
  fixture labels do not launch Chromium, so it reports browser RSS as not
  applicable; browser startup is covered by the composed runtime readiness gate.
- Provider smokes and the competitive Firecrawl evaluation require private
  credentials and are not run by public CI.
- No automatic migration is provided for an already-running legacy crawl.
