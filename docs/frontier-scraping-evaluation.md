# Frontier web-scraping evaluation

**Evaluation date:** 2026-07-21

**Local service version:** CrawlTrove 0.2.1
**Repository baseline:** `202d60196096d6e53ad58e5862d59a2d622f6d56`

This document evaluates CrawlTrove's acquisition layer against current
web-scraping platforms and open-source crawlers. It separates three kinds of
evidence:

- **Observed implementation:** behavior present in the local source.
- **Observed runtime:** behavior measured against the running Docker service.
- **Documented external capability:** behavior described by a competitor's
  official documentation. No paid competitor calls were made.

The comparison is intentionally acquisition-focused. Retrieval, research, and
corpus governance are included where they change the product boundary, but they
are not treated as substitutes for reliable fetching.

## Summary

CrawlTrove is a strong private corpus and research system with a capable but not
frontier-grade acquisition engine.

Its clearest strengths are durable artifacts, PDF/EPUB/image ingestion,
license/language/quality/deduplication signals, restart-safe jobs, Postgres
history, and local hybrid retrieval. Those facilities go beyond the normal
scope of scraping libraries.

Its clearest acquisition gaps are browser latency, post-render block detection,
persistent sessions, proxy and fingerprint management, CAPTCHA handling,
retry/fallback orchestration, and large-crawl controls. Bright Data and
Browserbase are substantially stronger managed browser platforms. Firecrawl
Cloud has a broader managed acquisition API. Crawl4AI and Scrapling expose more
advanced local crawler and anti-blocking controls.

### Interpretive scorecard

These scores summarize the evidence below; they are product-analysis judgments,
not standardized benchmark results.

| Layer | Score | Assessment |
| --- | ---: | --- |
| Static HTTP acquisition | 7/10 | Fast Chrome-impersonating HTTP tier. |
| JavaScript rendering | 5/10 | Renders dynamic pages, but the current browser path has a severe latency defect. |
| Anti-bot and unblocking | 2/10 | Basic challenge strings and Playwright stealth; no proxy/CAPTCHA/fingerprint system. |
| Crawl orchestration | 6/10 | Useful bounded local crawler with checkpoints; limited controls and scale. |
| Extraction and documents | 8/10 | Strong Markdown, PDF, EPUB, image OCR, and optional schema extraction. |
| Durable corpus and retrieval | 9/10 | CrawlTrove's strongest differentiation. |
| Production browser infrastructure | 3/10 | No reusable sessions, browser profiles, CDP, or distributed browser pool. |

As a pure web-acquisition product, CrawlTrove is currently mid-tier. As a
private corpus platform, it is considerably stronger.

## Local capability baseline

### Observed implementation

- `app/fetch.py` performs HTTP fetches with `curl_cffi` and a Chrome TLS
  fingerprint before considering Playwright.
- `app/scraper.py` renders pages with Chromium, applies Playwright stealth,
  supports ordered `wait`, `click`, `scroll`, `fill`, and `press` actions, and
  captures raw HTML and a screenshot.
- HTML extraction uses `trafilatura` with a BeautifulSoup/`markdownify`
  fallback.
- The document pipeline parses PDF, EPUB, and image inputs and can escalate OCR
  beyond Tesseract when a vision backend is configured.
- The crawler uses sitemap seeding, same-domain traversal, three workers,
  change/deduplication signals, raw artifacts, and restart checkpoints.
- Public crawl limits are 100 pages, depth 5, and 50 explicit batch URLs. Map
  requests return at most 5,000 links.
- Postgres-backed jobs provide scheduling, run history, records, full-text
  search, and JSON/CSV exports.
- Semantic, keyword, and hybrid retrieval operate over persisted artifacts and
  offline corpus records.

These are useful local-operator features, but they are not equivalent to a
managed proxy network, a distributed browser fleet, or a web-scale index.

## Runtime benchmark

The following calls exercised the running CrawlTrove 0.2.1 Docker service. The
fixtures were selected to cover static HTML, a JavaScript page, a simple
interaction, a challenge page, a document, plain text, mapping, crawling, and
schema extraction.

| Case | Result | Wall time | Observation |
| --- | --- | ---: | --- |
| `https://example.com`, `engine=auto` | Passed | 61.731 s | Auto escalated a valid short page to Chromium. |
| Same URL, `engine=http` | Passed | 0.211 s | Returned the same 183-character Markdown without a browser. |
| `https://quotes.toscrape.com/js/` | Passed | 64.467 s | Rendered the JavaScript quotes and found the expected content. |
| W3C dummy PDF | Passed | 0.926 s | Extracted `Dummy PDF file` through the document pipeline. |
| Firecrawl `llms.txt`, forced HTTP | Passed | 2.682 s | Preserved 23,860 characters of Markdown. |
| Map `books.toscrape.com`, limit 30 | Passed | 3.470 s | Returned 30 links. |
| Crawl `books.toscrape.com`, five pages | Passed | 3.995 s | Five results, zero errors, 9,726 Markdown characters. |
| Dynamic loading click and selector wait | Partial | 68.621 s | Click succeeded; the five-second selector wait reported failure, although expected content appeared before capture. |
| `https://nowsecure.nl/` challenge page | False success | 65.562 s | Returned `success=true` while Cloudflare Turnstile verification remained present. |
| JSON-Schema extraction | Failed | 1.545 s | The configured deployment returned HTTP 502: `Extraction failed: Connection error`. |

The full local test suite passed during the evaluation:

```text
485 passed in 16.00s
```

The suite is broad at the API, persistence, corpus, and pure-function layers,
but browser actions are primarily tested through fake page objects. The
browser defects below therefore remain outside the current regression gate.

## Critical findings

### P0: missing metadata causes about 60 seconds of browser latency

**Observed runtime:** an isolated Playwright launch, navigation, screenshot, and
close completed in 1.647 seconds. Running the same URL through
`WebScraper.scrape(..., engine="browser")` took 61.77 seconds.

**Observed implementation:** `WebScraper.scrape` calls
`get_attribute("content")` for `meta[name='description']`, then repeats the call
for `meta[property='og:description']` when the first element is absent. Those
optional locator calls inherit Playwright's default wait behavior. A page that
contains neither element pays two long waits.

This defect dominates browser scrape latency on otherwise simple pages.
Metadata reads should use immediate DOM evaluation, an existence check, or a
short explicit timeout.

### P0: rendered challenge pages can be reported as successful content

**Observed runtime:** CrawlTrove returned a successful 74-character Markdown
result for the challenge fixture. Independent browser inspection showed two
unchecked Cloudflare `Verify you are human` widgets on the rendered page.

**Observed implementation:** the HTTP tier recognizes a small set of challenge
strings before browser escalation. After rendering, the pipeline does not run a
comparable structural block classifier. Forms and iframes are then removed from
the cleaned output, which can erase the strongest evidence of the challenge.

A blocked page should produce a distinct blocked result or `success=false`, with
reason and attempt metadata. Silently indexing a challenge shell is more
harmful than returning an explicit acquisition failure.

### P0: automatic browser escalation is too coarse

`app/fetch.py::needs_browser` escalates any non-HTML content type and any page
with fewer than 400 visible characters. This causes at least two avoidable
behaviors:

- Valid short HTML pages are rendered even when their HTTP content is complete.
- Plain text, Markdown, JSON, XML, and feed responses are sent to a browser even
  though the HTTP bytes are already the desired source.

The decision should account for content type, status, meaningful text,
script-to-content ratio, known shell structure, and challenge markers. A fixed
text-length threshold should not be sufficient by itself.

### P1: MCP reliability needs an isolated concurrency test

Concurrent MCP scrape attempts timed out at the client limit while direct HTTP
API calls and health checks remained responsive. Direct API benchmarks
completed normally.

This suggests adapter/client contention or serialization rather than a general
service outage, but the evaluation did not isolate the exact layer. Add a test
that runs multiple MCP calls against the same service, records server completion
and response-body size, and distinguishes scraper latency from stdio transport
latency.

## Capability comparison

The table below combines observed CrawlTrove implementation with currently
documented external features. External entries are documentation-grounded and
may include vendor claims.

| Capability | CrawlTrove | Current frontier examples |
| --- | --- | --- |
| HTTP-first fetching | Chrome-impersonating HTTP before Playwright. | Common in Crawl4AI, Scrapling, Firecrawl, and managed unlockers. |
| Browser interaction | New Chromium session; five action types; raw screenshot. | Firecrawl Interact adds prompting, code execution, profiles, live view, and CDP. Stagehand exposes `act`, `extract`, `observe`, and agent workflows. |
| Anti-bot handling | Static markers plus Playwright stealth. | Bright Data documents residential proxy rotation, fingerprint management, CAPTCHA solving, retries, and recovery. Browserbase documents proxies, CAPTCHA solving, authentication, and verified browser identities. |
| Block classification | HTTP marker checks; no robust post-render classifier. | Crawl4AI documents structural block detection, proxy retries, fallback fetchers, attempt statistics, and explicit failure when blocking remains. |
| Sessions and identity | No public persistent cookie/profile/session abstraction. | Browserbase, Firecrawl Interact, Bright Data, Crawl4AI, and Scrapling document persistent or reusable sessions. |
| Crawl controls | Same-domain traversal, sitemap seed, three workers, limit 100/depth 5. | Crawl4AI documents deep/adaptive strategies and resumable state. Scrapling documents per-domain throttling, multi-session spiders, pause/resume, proxy rotation, and robots controls. Crawlee/Apify adds autoscaled queues and managed storage. |
| Structured extraction | Optional LLM extraction against JSON Schema. | Firecrawl and Stagehand provide managed schema extraction. Crawl4AI also documents deterministic CSS/XPath and LLM-free strategies. |
| Documents | PDF, EPUB, direct images, Tesseract, optional vision escalation. | Firecrawl Parse documents a broader office-file surface. Crawl4AI documents PDF parsing. Many crawler libraries leave documents to application code. |
| Persistence and resume | Raw artifacts, Postgres runs, crawl/research checkpoints, exports. | Crawl4AI, Scrapling, and Crawlee provide cache or resume primitives; cloud products retain managed jobs. |
| Corpus governance | License, language, quality, deduplication, change, and provenance signals. | Usually absent or shallow in acquisition-focused products. |
| Retrieval and research | Local semantic/keyword/hybrid retrieval and budgeted research jobs. | Firecrawl overlaps through Search, Agent, and Monitor, but this is not the same as an operator-owned local corpus. |

## Relationship by product layer

- **Firecrawl:** direct acquisition and extraction competitor; a possible
  upstream provider for difficult pages. CrawlTrove remains differentiated by
  durable local corpus ownership and governance.
- **Bright Data Browser API and Web Unlocker:** stronger managed unblocking and
  browser infrastructure; best treated as an optional acquisition provider,
  not a replacement for CrawlTrove's corpus layer.
- **Browserbase and Stagehand:** complementary browser/session/agent
  infrastructure. They overlap browser automation but not CrawlTrove's durable
  corpus model.
- **Crawl4AI:** the closest direct open-source engine competitor, with broader
  browser, extraction, deep-crawl, proxy, and block-detection controls.
- **Scrapling:** a direct acquisition/crawler competitor, especially for
  adaptive selectors, stealth fetchers, multi-session spiders, proxy rotation,
  robots policy, and pause/resume.
- **Crawlee and Apify:** stronger for autoscaled crawler execution, queues,
  sessions, datasets, schedules, and managed proxy infrastructure; weaker on
  CrawlTrove's corpus semantics.

## Recommended roadmap

### P0: correctness and latency

1. Replace waiting metadata locators with immediate, bounded reads.
2. Add structural post-render block detection and explicit blocked outcomes.
3. Make automatic browser escalation content-type and structure aware.
4. Add real-browser regression fixtures for static HTML, SPA rendering, delayed
   interaction, plain text, redirects, challenge pages, and browser timing.

### P1: acquisition controls

5. Expose headers, cookies, locale, timezone, geolocation, viewport, user agent,
   resource blocking, `waitUntil`, selector waits, and navigation timeouts.
6. Add reusable named sessions/browser profiles instead of launching one browser
   per scrape.
7. Add proxy configuration and rotation, retry/backoff policy, blocked-attempt
   telemetry, and a fallback-fetch interface.
8. Add crawl include/exclude rules, explicit robots policy, per-domain pacing,
   adaptive concurrency, and configurable retries.
9. Isolate and benchmark MCP concurrency independently of the HTTP API.

### P2: differentiated expansion

10. Add optional acquisition adapters for Bright Data, Browserbase, Firecrawl,
    or a user-supplied fetch function. Keep storage, governance, indexing, and
    retrieval local.
11. Add deterministic CSS/XPath/schema extraction alongside LLM extraction.
12. Expand office-document handling only when corpus demand justifies the added
    dependencies and attack surface.

## Product boundary

CrawlTrove should be positioned as the private system of record for acquired web
knowledge, not as a replacement for a global proxy network or hosted browser
fleet.

The strongest architecture is a pluggable acquisition layer:

1. Use inexpensive local HTTP acquisition for ordinary pages.
2. Escalate to local Playwright when rendering or simple interaction is needed.
3. Optionally route genuinely protected targets to a specialized provider.
4. Normalize, preserve, govern, index, and retrieve all accepted content through
   CrawlTrove.

That boundary preserves CrawlTrove's strongest differentiation while allowing
frontier acquisition providers to complement it instead of forcing the project
to reproduce their infrastructure.

## Sources

### Local evidence

- `README.md`
- `docs/http-api.md`
- `app/fetch.py::needs_browser`
- `app/scraper.py::WebScraper.scrape`
- `app/crawler.py::WebCrawler.run_crawl`
- `app/actions.py`
- `app/documents/`
- `app/storage.py`
- `app/retrieval.py`
- `app/vecindex.py`
- `tests/`

### Official external documentation

Accessed 2026-07-21:

- [Firecrawl Interact](https://docs.firecrawl.dev/features/interact)
- [Firecrawl documentation index](https://docs.firecrawl.dev/llms.txt)
- [Bright Data Browser API](https://docs.brightdata.com/scraping-automation/scraping-browser/introduction)
- [Browserbase Agent Identity](https://docs.browserbase.com/platform/identity/overview)
- [Stagehand documentation](https://docs.stagehand.dev/)
- [Crawl4AI anti-bot and fallback](https://docs.crawl4ai.com/advanced/anti-bot-and-fallback/)
- [Crawl4AI adaptive crawling](https://docs.crawl4ai.com/core/adaptive-crawling/)
- [Crawl4AI deep crawling](https://docs.crawl4ai.com/core/deep-crawling/)
- [Scrapling documentation](https://scrapling.readthedocs.io/en/latest/)
- [Crawlee documentation](https://crawlee.dev/)

Vendor descriptions of success rate, scale, stealth, bypass, or reliability are
vendor claims unless reproduced in the runtime benchmark above. Pricing and
plan limits were omitted because they are volatile and were not needed for the
architecture decision.
