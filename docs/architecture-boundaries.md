# Architecture boundaries (CrawlTrove)

**Status:** normative for service code  
**Date:** 2026-07-24

This note fences the dual-path and silent-failure debt left after the v0.4 durable crawl migration. It is the companion to the executable guards in `tests/test_architecture_invariants.py`.

## 1. Two crawl control planes — do not mix

| Plane | Package | Durability | Who may use it |
| --- | --- | --- | --- |
| **Durable (default)** | `app/crawl/*`, `crawl_service` | Postgres jobs/tasks, leases, remote workers | All new crawl product work, scheduler, remote workers |
| **Legacy (compat)** | `app/crawler.WebCrawler` via `app.services.crawler` | In-memory + optional file checkpoints | `/api/llmstxt`; resume of pre-v0.4 interrupted jobs only |

**Rules:**

1. New features that enqueue or execute crawls MUST call `app.crawl.service.submit_crawl` (or the repository APIs behind it).
2. Only `app/crawler.py` and `app/services.py` may `import app.crawler` / `WebCrawler`. Other modules receive the singleton through `app.services` or stay on the durable path.
3. HTTP status/resume endpoints may **read** legacy jobs for compatibility, but must not grow new legacy write paths.
4. Flip condition for deleting `WebCrawler`: migrate `/api/llmstxt` to durable crawl (or drop the endpoint) and ship a one-shot checkpoint → durable import if any customers still need it.

## 2. Soft-fail vs control-plane fail

| Kind | Behavior | Examples |
| --- | --- | --- |
| **Corpus signals** | Never drop the page; record `signal_errors` | quality, lang, license, OCR vision |
| **Retrieval signals** | May return fewer hits; MUST log + count degradations | embed, semantic index, Postgres FTS |
| **Control plane** | Fail closed on readiness; log with reason codes | crawl service start, worker register, lease maintenance, migrations |

Empty `except: pass` on control-plane start/stop or paid provider session cleanup is forbidden. Loop-level catch is OK when it logs, updates counters, and surfaces `last_error` / readiness reasons.

## 3. Public APIs across packages

Prefer package-public helpers over private `_` attributes:

| Need | Public entry |
| --- | --- |
| Wake durable worker | `crawl_service.wake()` |
| Maintenance / readiness snapshot | `crawl_service.maintenance_status()`, `leases_ready()` |
| Migration version list | `migrate.migration_versions()` |
| PDF page bitmap for OCR/vision | `ocr.render_page(page, dpi)` |

## 4. Observability hooks added for residual soft paths

- `crawltrove_retrieval_degradations_total{signal=...}` — embed / semantic / keyword_db / keyword failures that degraded instead of 5xx.
- Remote worker readiness may include `reasons` (component → short diagnostic) when a probe is not healthy.

## 5. Revisit when

- **Drop legacy crawler** when llmstxt is durable (or removed) and no checkpoint resume traffic remains for one release cycle.
- **Raise retrieval soft-fail to hard fail** only if product needs strict “empty means error” semantics for a named API consumer (metric: degradation rate on the serving path).
