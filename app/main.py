import asyncio
import datetime
import html as html_lib
import json
import logging
from fastapi import FastAPI, BackgroundTasks, HTTPException, status, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
import os
import sys
import base64
import secrets
from pathlib import Path

from app import corpus_browser, dedup, embeddings, extract_llm, fetch, llmstxt, log, normalize, research, retrieval, search, sitemap, storage, scheduler, runner, vecindex
from app.services import scraper, crawler, researcher, batcher
from app.db import migrate, pool, repo
from app.crawl.service import crawl_service
from app.routes import (
    routes_acquisition_sessions,
    routes_crawl,
    routes_export,
    routes_jobs,
    routes_operations,
    routes_runs,
)
from app.url_safety import UnsafeUrlError

logger = logging.getLogger("main")

VERSION = Path(__file__).with_name("VERSION").read_text(encoding="utf-8").strip()


class ArtifactStaticFiles(StaticFiles):
    """Serve stored artifacts without executing captured page source."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["X-Content-Type-Options"] = "nosniff"
        if path.lower().endswith((".html", ".html.txt")):
            response.headers["Content-Type"] = "text/plain; charset=utf-8"
            filename = os.path.basename(path).replace('"', "")
            response.headers["Content-Disposition"] = (
                f'attachment; filename="{filename}"'
            )
        return response

app = FastAPI(
    title="CrawlTrove API",
    description="Self-hosted web scraping and crawling into clean Markdown",
    version=VERSION,
)

# Same-origin dashboard requests need no CORS. Cross-origin browser access is
# opt-in through an explicit allowlist; wildcard origins never receive cookies.
CORS_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CORS_ORIGINS", "").split(",")
    if origin.strip()
]
if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials="*" not in CORS_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.exception_handler(UnsafeUrlError)
async def unsafe_url_handler(_request: Request, exc: UnsafeUrlError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})

# Optional auth gate. Two credentials, either sufficient:
#   * HTTP Basic Auth via APP_USERNAME/APP_PASSWORD (browser dashboard).
#   * X-API-Key via API_KEYS (comma-separated) for programmatic clients.
# The gate is enabled only when at least one is configured (local/dev stays
# open). Every route is protected except the health check, which must stay open
# and 200 for platform probes.
APP_USERNAME = os.environ.get("APP_USERNAME", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD")
API_KEYS = {k.strip() for k in os.environ.get("API_KEYS", "").split(",") if k.strip()}
_AUTH_OPEN_PATHS = {"/api/health", "/health/live", "/health/ready"}


def _auth_configured() -> bool:
    return bool(APP_PASSWORD) or bool(API_KEYS)


def _authorized(request: Request) -> bool:
    # X-API-Key: constant-time compare against any configured key.
    if API_KEYS:
        key = request.headers.get("x-api-key", "")
        if key and any(secrets.compare_digest(key, k) for k in API_KEYS):
            return True
    # HTTP Basic Auth.
    if APP_PASSWORD:
        header = request.headers.get("authorization", "")
        if header.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
                if (secrets.compare_digest(user, APP_USERNAME)
                        and secrets.compare_digest(pw, APP_PASSWORD)):
                    return True
            except Exception:
                pass
    return False


@app.middleware("http")
async def require_auth(request: Request, call_next):
    session_bearer_path = (
        request.url.path.startswith("/api/acquisition/sessions/")
        and (request.url.path.endswith("/open") or "/screenshots/" in request.url.path)
    )
    metrics_open = (
        request.url.path == "/metrics"
        and routes_operations.metrics_auth_bypass_allowed()
    )
    if (_auth_configured() and request.method != "OPTIONS"
            and request.url.path not in _AUTH_OPEN_PATHS
            and not metrics_open and not session_bearer_path):
        if not _authorized(request):
            headers = {}
            if APP_PASSWORD:  # only meaningful for the Basic scheme
                headers["WWW-Authenticate"] = 'Basic realm="CrawlTrove"'
            return Response(status_code=401, headers=headers)
    return await call_next(request)


def _bind_host() -> str:
    """Best-effort detection of the externally reachable bind address."""
    published_host = os.environ.get("PUBLISHED_BIND_ADDRESS")
    if published_host:
        return published_host
    argv = sys.argv
    for i, a in enumerate(argv):
        if a == "--host" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--host="):
            return a.split("=", 1)[1]
    return os.environ.get("HOST") or os.environ.get("UVICORN_HOST") or "127.0.0.1"


def _is_loopback_host(host: str) -> bool:
    return (host or "").strip().lower() in ("", "127.0.0.1", "::1", "localhost")


def _allow_unauthenticated() -> bool:
    return os.environ.get("ALLOW_UNAUTHENTICATED", "").lower() in ("1", "true", "yes")


def check_bind_policy(*, host: str, has_auth: bool, allow_unauth: bool) -> str:
    """LAN-hardening guard. Binding to a non-loopback host with no
    auth configured is refused (raise) unless ALLOW_UNAUTHENTICATED downgrades it
    to a loud warning. Returns 'ok', 'warned', or raises RuntimeError."""
    if _is_loopback_host(host) or has_auth:
        return "ok"
    detail = (f"bound to non-loopback host {host!r} with no authentication "
              f"configured — set APP_PASSWORD (Basic auth) or API_KEYS "
              f"(X-API-Key) to secure it")
    if allow_unauth:
        logger.warning("SECURITY: serving %s (ALLOW_UNAUTHENTICATED=true)", detail)
        return "warned"
    raise RuntimeError(
        f"Refusing to start: {detail}, or set ALLOW_UNAUTHENTICATED=true to run "
        f"open anyway.")

# scraper / crawler are shared singletons from app.services (also used by the
# job runner + scheduler), so a scheduled crawl stays visible via /api/crawl.

# Ensure static directories exist
os.makedirs("app/static", exist_ok=True)

# Persistence + lifecycle routes (no-op surface when DATABASE_URL is unset —
# the routers themselves return 503 in that case).
app.include_router(routes_jobs.router)
app.include_router(routes_runs.router)
app.include_router(routes_export.router)
app.include_router(routes_crawl.router)
app.include_router(routes_acquisition_sessions.router)
app.include_router(routes_acquisition_sessions.tunnel_router)
app.include_router(routes_operations.health_router)
app.include_router(routes_operations.metrics_router)
app.include_router(routes_operations.router)


@app.on_event("startup")
async def _on_startup():
    """Restore interrupted research runs and crawl checkpoints, then apply
    migrations and start the scheduler.

    Both restores are file-based and NOT gated on DATABASE_URL — checkpoints
    are the source of truth. Research auto-resume (RESEARCH_RESUME_ON_START,
    default on) needs a configured LLM backend; crawl auto-resume
    legacy crawl checkpoints remain interrupted for the explicit compatibility
    resume endpoint; durable jobs recover through PostgreSQL leases.
    DB-side startup is entirely skipped (legacy behaviour) when DATABASE_URL is
    unset; migration failures are logged but never prevent serving scrapes.
    """
    log.configure_logging()
    # Refuse to serve a non-loopback bind without auth
    # unless explicitly allowed. Runs first so a misconfig fails fast.
    check_bind_policy(host=_bind_host(), has_auth=_auth_configured(),
                      allow_unauth=_allow_unauthenticated())
    try:
        restored = researcher.restore_from_checkpoints()
        resume_on_start = os.environ.get(
            "RESEARCH_RESUME_ON_START", "true").lower() in ("1", "true", "yes")
        if restored and resume_on_start and extract_llm.configured():
            for job_id in restored[:research.MAX_CONCURRENT]:
                researcher.resume_research(job_id)
                logger.info("auto-resumed research run %s", job_id)
        elif restored:
            logger.info("restored %d interrupted research run(s); not auto-resuming",
                        len(restored))
    except Exception as e:
        logger.error("research checkpoint restore failed on startup: %s", e)
    try:
        restored = crawler.restore_from_checkpoints()
        if restored:
            logger.info(
                "restored %d interrupted legacy crawl(s); explicit resume required",
                len(restored),
            )
    except Exception as e:
        logger.error("crawl checkpoint restore failed on startup: %s", e)
    if not pool.enabled():
        return
    try:
        await migrate.run_migrations()
    except Exception as e:
        logger.error("migrations failed on startup: %s", e)
        return
    try:
        if await pool.get_pool() is not None:
            await crawl_service.start()
    except Exception:
        # Keep serving scrape/health, but never hide control-plane failure.
        # Readiness fails closed via crawl_service.maintenance_status().
        logger.exception("durable crawl service failed to start on startup")
    app.state.scheduler_task = asyncio.create_task(scheduler.scheduler_loop())


@app.on_event("shutdown")
async def _on_shutdown():
    await crawl_service.stop()
    task = getattr(app.state, "scheduler_task", None)
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
    for runtime_owner in (scraper, crawler.scraper, researcher.scraper, batcher.scraper):
        await runtime_owner.close()
    await fetch.close_http_fetcher()
    await pool.reset_pool()

# Pydantic schemas (compatible with Pydantic v2)
class Action(BaseModel):
    type: str = Field(..., pattern="^(wait|click|scroll|fill|press)$", description="Interaction kind, executed in order on the rendered page")
    selector: Optional[str] = Field(None, description="CSS selector (click/fill, or wait-for-selector)")
    milliseconds: Optional[int] = Field(None, ge=0, le=10000, description="wait: fixed delay")
    text: Optional[str] = Field(None, description="fill: the text to type")
    key: Optional[str] = Field(None, description="press: the keyboard key (e.g. Enter)")
    direction: Optional[str] = Field(None, pattern="^(up|down)$", description="scroll: direction (default down)")

class ScrapeRequest(BaseModel):
    url: str = Field(..., description="The absolute URL of the page to scrape")
    waitForMs: int = Field(1000, alias="waitForMs", ge=0, le=10000, description="Time in milliseconds to wait for JS rendering")
    onlyMainContent: bool = Field(True, description="Attempt to clean headers/footers/nav and extract only main content")
    engine: str = Field("auto", pattern="^(auto|http|browser)$", description="auto = cheap HTTP fetch first, escalate to Playwright only if needed; http/browser force one tier")
    actions: Optional[List[Action]] = Field(None, max_length=20, description="Pre-capture page actions (wait/click/scroll/fill/press); implies the browser tier")

    model_config = {
        "populate_by_name": True,
        "json_schema_extra": {
            "example": {
                "url": "https://news.ycombinator.com",
                "waitForMs": 1000,
                "onlyMainContent": True
            }
        }
    }

class BatchScrapeRequest(BaseModel):
    urls: List[str] = Field(..., min_length=1, max_length=50, description="URLs to scrape (each runs the normal scrape pipeline)")
    waitForMs: int = Field(1000, alias="waitForMs", ge=0, le=10000, description="Per-page JS render wait")
    onlyMainContent: bool = Field(True, description="Clean headers/footers/nav on every page")
    engine: str = Field("auto", pattern="^(auto|http|browser)$", description="Fetch tier per page")

    model_config = {
        "populate_by_name": True,
        "json_schema_extra": {
            "example": {
                "urls": ["https://example.com/a", "https://example.com/b"],
                "onlyMainContent": True
            }
        }
    }

class LlmstxtRequest(BaseModel):
    url: str = Field(..., description="The site to generate llms.txt for")
    maxUrls: int = Field(10, alias="maxUrls", ge=1, le=50, description="Maximum pages crawled for the index")
    useSitemap: bool = Field(True, description="Seed the crawl from robots.txt/sitemap.xml")

    model_config = {
        "populate_by_name": True,
        "json_schema_extra": {"example": {"url": "https://quotes.toscrape.com", "maxUrls": 10}}
    }

class WebSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, description="The web search query")
    limit: int = Field(8, ge=1, le=20, description="Maximum number of results")

    model_config = {
        "populate_by_name": True,
        "json_schema_extra": {"example": {"query": "swift 6.4 concurrency", "limit": 8}}
    }

class MapRequest(BaseModel):
    url: str = Field(..., description="The base URL of the site to map")
    search: Optional[str] = Field(None, description="Case-insensitive substring filter applied to discovered URLs")
    limit: int = Field(100, ge=1, le=5000, description="Maximum number of links returned")
    sitemapOnly: bool = Field(False, description="Use only sitemap discovery; skip the shallow link pass over the base page")

    model_config = {
        "populate_by_name": True,
        "json_schema_extra": {
            "example": {
                "url": "https://quotes.toscrape.com",
                "search": "author",
                "limit": 100
            }
        }
    }

class ExtractRequest(BaseModel):
    url: str = Field(..., description="The URL to scrape and extract from")
    schema_: Dict[str, Any] = Field(..., alias="schema", description="JSON Schema the extracted data must conform to")
    prompt: str = Field("", description="Optional extraction instructions")
    examples: Optional[List[Dict[str, Any]]] = Field(
        None, description="Optional few-shot exemplars, each {\"markdown\": <page text>, \"output\": <expected object>}, injected as prior turns to steer value choices and list cardinality")
    engine: str = Field("auto", pattern="^(auto|http|browser)$")
    model: str = Field(extract_llm.DEFAULT_MODEL, description="Claude model id to use")
    waitForMs: int = Field(1000, alias="waitForMs", ge=0, le=10000)

    model_config = {
        "populate_by_name": True,
        "json_schema_extra": {
            "example": {
                "url": "https://example.com/song",
                "schema": {
                    "type": "object",
                    "properties": {
                        "artist": {"type": "string"},
                        "work": {"type": "string"},
                        "license": {"type": ["string", "null"]}
                    },
                    "required": ["artist", "work", "license"]
                },
                "prompt": "Extract the musical work and its license."
            }
        }
    }


class ResearchRequest(BaseModel):
    query: str = Field(..., min_length=3, description="The research question")
    maxRounds: int = Field(4, alias="maxRounds", ge=1, le=10, description="Max search→read→assess rounds")
    maxPages: int = Field(25, alias="maxPages", ge=1, le=100, description="Max pages scraped across the run")
    maxMinutes: int = Field(30, alias="maxMinutes", ge=1, le=120, description="Wall-clock budget")
    seedUrls: Optional[List[str]] = Field(None, alias="seedUrls", description="Optional starting URLs, read in round 1")

    model_config = {
        "populate_by_name": True,
        "json_schema_extra": {
            "example": {"query": "Compare AVAudioEngine tap APIs across iOS 26 and 27",
                        "maxRounds": 4, "maxPages": 25}
        }
    }


@app.get("/api/health")
async def health_check():
    """Liveness + DB state for uptime probes.

    ALWAYS 200 and auth-exempt (the DB is optional) so a probe never flaps on a
    transient database hiccup. `db` is 'disabled' (no DATABASE_URL), 'up'
    (reachable), or 'down' (configured but unreachable).
    """
    if not pool.enabled():
        db_state = "disabled"
    else:
        db_state = "up" if await pool.ping() else "down"
    return {"status": "healthy", "service": "crawltrove",
            "version": VERSION, "db": db_state,
            "providers": crawl_service.registry.health()}

@app.post("/api/scrape")
async def scrape_url(request: ScrapeRequest):
    """Scrapes a single URL and returns clean markdown and structural page data."""
    if not request.url.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="URL must start with http:// or https://"
        )
    
    result = await scraper.scrape(
        url=request.url,
        wait_for_ms=request.waitForMs,
        only_main_content=request.onlyMainContent,
        engine=request.engine,
        actions=[a.model_dump(exclude_none=True) for a in request.actions]
        if request.actions else None
    )

    if not result["success"]:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to scrape URL: {result.get('error')}"
        )

    # Strip the private raw-capture channel (verbatim html + screenshot bytes)
    # before the result is returned or JSON-saved; it's persisted to files below.
    raw = result.pop("_raw", {}) or {}

    # Dedup-flag, persist artifact + raw capture, and index the run/page
    # (additive; gated on DATABASE_URL; swallowed — never changes this response).
    persisted = await runner.persist_scrape_page(result, raw, trigger="manual")
    stem = persisted["stem"]
    try:
        if persisted["run_id"] is not None:
            await repo.record_run_finish(
                persisted["run_id"], status="completed",
                engine_used=result.get("metadata", {}).get("engine"),
                pages_count=1,
                raw_output_path=(f"data/scrapes/{stem}.json" if stem else None),
            )
    except Exception as e:
        logger.warning("failed to finish scrape run: %s", e)

    return result

@app.post("/api/extract")
async def extract_structured(request: ExtractRequest):
    """Scrapes a URL and extracts schema-shaped structured data via Claude."""
    if not extract_llm.configured():
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="LLM extraction is not configured — set ANTHROPIC_API_KEY (or LOCAL_LLM_BASE_URL / AI_GATEWAY_API_KEY) in the service environment."
        )
    if not request.url.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="URL must start with http:// or https://"
        )

    scraped = await scraper.scrape(
        url=request.url,
        wait_for_ms=request.waitForMs,
        only_main_content=True,
        engine=request.engine
    )
    if not scraped["success"]:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to scrape URL: {scraped.get('error')}"
        )

    raw = scraped.pop("_raw", {}) or {}

    try:
        extracted = await extract_llm.extract(
            scraped["markdown"], request.url, request.schema_,
            prompt=request.prompt, model=request.model, examples=request.examples
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Extraction failed: {e}"
        ) from e

    # Persist the page (shared with /scrape) and the structured records it
    # yielded. Additive + swallowed; never changes this response.
    try:
        persisted = await runner.persist_scrape_page(scraped, raw, trigger="manual")
        page_id, run_id, stem = (
            persisted["page_id"], persisted["run_id"], persisted["stem"])
        rows = normalize.record_rows_from_extract(extracted, request.url)
        for i, row in enumerate(rows):
            try:
                # Record-level content_hash via the shared dedup helper, under a
                # record-scoped key so it never clobbers the page's url entry in
                # the LSH index.
                row["content_hash"] = dedup.check_and_register(
                    json.dumps(row["data_json"], sort_keys=True,
                               ensure_ascii=False, default=str),
                    key=f"record::{request.url}::{row['record_type']}::{i}",
                )["content_hash"]
            except Exception as e:
                logger.warning("record dedup failed: %s", e)
            await repo.record_extracted_record(page_id, **row)
        if run_id is not None:
            await repo.record_run_finish(
                run_id, status="completed",
                engine_used=scraped.get("metadata", {}).get("engine"),
                pages_count=1,
                raw_output_path=(f"data/scrapes/{stem}.json" if stem else None),
            )
    except Exception as e:
        logger.warning("failed to record extract run: %s", e)

    return {
        "success": True,
        "url": request.url,
        "data": extracted["data"],
        "model": extracted["model"],
        "usage": extracted["usage"],
        "metadata": scraped["metadata"],
    }


@app.post("/api/llmstxt", status_code=status.HTTP_202_ACCEPTED)
async def start_llmstxt(request: LlmstxtRequest, background_tasks: BackgroundTasks):
    """Generate llms.txt + llms-full.txt via a bounded durable crawl.

    Requires PostgreSQL (same as POST /api/crawl). Returns 202 + jobId; poll
    GET /api/llmstxt/{jobId} until ``llmstxt`` is present (or ``error``).
    """
    from uuid import UUID

    from app.acquisition.registry import ProviderUnavailable
    from app.crawl.config import CrawlConfig
    from app.crawl.repository import PersistenceUnavailable
    from app.crawl.service import ProviderBudgetInvalid

    if not request.url.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="URL must start with http:// or https://",
        )
    config = CrawlConfig(
        url=request.url,
        limit=request.maxUrls,
        maxDepth=2,
        onlyMainContent=True,
        engine="auto",
        useSitemap=request.useSitemap,
    )
    try:
        job_id = await crawl_service.submit_crawl(config)
    except UnsafeUrlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PersistenceUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "persistence_unavailable",
                "message": "llms.txt generation requires PostgreSQL",
            },
        ) from exc
    except ProviderUnavailable as exc:
        raise HTTPException(status_code=503, detail={
            "code": "provider_unavailable",
            "message": "Requested provider is unavailable",
        }) from exc
    except ProviderBudgetInvalid as exc:
        raise HTTPException(status_code=422, detail={
            "code": "provider_budget_invalid",
            "message": str(exc),
        }) from exc
    uid = job_id if isinstance(job_id, UUID) else UUID(str(job_id))
    background_tasks.add_task(llmstxt.run_for_job, uid)
    return {"success": True, "jobId": str(job_id)}


@app.get("/api/llmstxt/{job_id}")
async def get_llmstxt_status(job_id: str):
    """Progress of an llms.txt generation; includes the llms.txt text inline
    (and both file paths) once formatting finishes."""
    from uuid import UUID

    from app.crawl import repository as crawl_repository
    from app.crawl.repository import PersistenceUnavailable

    try:
        durable_id = UUID(job_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"llms.txt job with ID '{job_id}' not found.",
        )
    try:
        job = await crawl_repository.get_job(durable_id)
    except PersistenceUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "persistence_unavailable",
                "message": "llms.txt generation requires PostgreSQL",
            },
        ) from exc
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"llms.txt job with ID '{job_id}' not found.",
        )
    return llmstxt.status_payload(job, job_id)


@app.post("/api/search/web")
async def search_web(request: WebSearchRequest):
    """Web search via the pluggable provider waterfall (SearXNG → Brave → DDG).

    Distinct from GET /api/search, which is Postgres full-text over already-
    scraped pages. Provider failures yield an empty result list, never a 5xx.
    """
    results = await search.search(request.query, n=request.limit)
    return {"success": True, "provider": search.provider(),
            "results": results[:request.limit], "count": len(results[:request.limit])}


def _artifact_paths(hit: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Map a semantic-index hit to browsable /data artifact paths.

    Crawl refs carry a ``#<pageIndex>`` suffix (one index row per page) that
    resolves to the single crawl artifact. Corpus records share a JSONL file, so
    their path comes from ``meta.file`` (data/-relative) — no .md sibling.
    """
    kind, ref = hit["kind"], hit["ref"]
    if str(ref).startswith("db:"):
        return {"json": None, "md": None}
    if kind == "crawl":
        stem = ref.split("#", 1)[0]
        return {"json": f"/data/crawls/{stem}.json", "md": f"/data/crawls/{stem}.md"}
    if kind == "scrape":
        return {"json": f"/data/scrapes/{ref}.json", "md": f"/data/scrapes/{ref}.md"}
    if kind == "research":
        return {"json": f"/data/research/{ref}.json", "md": f"/data/research/{ref}.md"}
    if kind == "corpus":
        f = (hit.get("meta") or {}).get("file")
        return {"json": (f"/data/{str(f).lstrip('/')}" if f else None), "md": None}
    return {"json": None, "md": None}


def _search_filters(namespace: Optional[str], bucket: Optional[str],
                    tier: Optional[str], framework: Optional[str]) -> Dict[str, str]:
    return {
        name: value.strip() for name, value in {
            "namespace": namespace, "bucket": bucket,
            "tier": tier, "framework": framework,
        }.items() if value and value.strip()
    }


def _search_filter_echo(kind: Optional[str], namespace: Optional[str],
                        bucket: Optional[str], tier: Optional[str],
                        framework: Optional[str]) -> Dict[str, Optional[str]]:
    return {
        "kind": kind,
        "namespace": namespace.strip() if namespace and namespace.strip() else None,
        "bucket": bucket.strip() if bucket and bucket.strip() else None,
        "tier": tier.strip() if tier and tier.strip() else None,
        "framework": framework.strip() if framework and framework.strip() else None,
    }


@app.get("/api/search/semantic")
async def search_semantic(q: str, kind: Optional[str] = None, k: int = 10,
                          namespace: Optional[str] = None,
                          bucket: Optional[str] = None,
                          tier: Optional[str] = None,
                          framework: Optional[str] = None):
    """Semantic (embedding) search over everything indexed: scrape artifacts,
    crawl pages, research reports, and corpus RAG records — tagged by ``kind``.

    Returns ``501`` when no embedding backend is configured (EMBEDDINGS_BASE_URL
    unset), mirroring /api/extract. A backend hiccup yields an empty result list,
    never a 5xx.
    """
    if not embeddings.configured():
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Semantic search is not configured — set EMBEDDINGS_BASE_URL "
                   "(+ EMBEDDINGS_MODEL) in the service environment.")
    q = (q or "").strip()
    if not q:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="query 'q' must not be empty")
    k = max(1, min(k, 50))
    if kind is not None and kind not in ("scrape", "crawl", "research", "corpus"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="kind must be one of: scrape, crawl, research, corpus")
    filters = _search_filters(namespace, bucket, tier, framework)
    if filters and kind not in (None, "corpus"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="namespace, bucket, tier, and framework filters require kind=corpus")
    try:
        hits = await retrieval.search(
            q, kind=kind, k=k, mode="semantic", filters=filters)
    except retrieval.RetrievalUnavailable as e:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED,
                            detail=str(e)) from e
    for h in hits:
        h.update(_artifact_paths(h))
    return {"success": True, "query": q, "kind": kind,
            "filters": _search_filter_echo(
                kind, namespace, bucket, tier, framework),
            "results": hits, "count": len(hits)}


@app.get("/api/search/semantic/stats")
async def search_semantic_stats():
    """Vector-index summary: total chunks, counts by kind, model + dimension,
    and whether the sqlite-vec extension is available in this process."""
    return {"success": True, "configured": embeddings.configured(),
            "index": vecindex.stats()}


@app.get("/api/search/hybrid")
async def search_hybrid(q: str, kind: Optional[str] = None, k: int = 10,
                        mode: str = "hybrid",
                        namespace: Optional[str] = None,
                        bucket: Optional[str] = None,
                        tier: Optional[str] = None,
                        framework: Optional[str] = None):
    """Hybrid, semantic-only, or keyword-only retrieval over indexed output."""
    q = (q or "").strip()
    if not q:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="query 'q' must not be empty")
    if kind is not None and kind not in ("scrape", "crawl", "research", "corpus"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="kind must be one of: scrape, crawl, research, corpus")
    if mode not in retrieval.MODES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="mode must be one of: hybrid, semantic, keyword")
    if not 1 <= k <= 50:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="k must be between 1 and 50")
    filters = _search_filters(namespace, bucket, tier, framework)
    if filters and kind not in (None, "corpus"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="namespace, bucket, tier, and framework filters require kind=corpus")
    try:
        hits = await retrieval.search(
            q, kind=kind, k=k, mode=mode, filters=filters)
    except retrieval.RetrievalUnavailable as e:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED,
                            detail=str(e)) from e
    for hit in hits:
        hit.update(_artifact_paths(hit))
    return {"success": True, "query": q, "kind": kind, "mode": mode,
            "filters": _search_filter_echo(
                kind, namespace, bucket, tier, framework),
            "results": hits, "count": len(hits)}


@app.get("/api/search/facets")
async def search_facets(q: str, kind: Optional[str] = None,
                        mode: str = "hybrid"):
    """Query-scoped facet counts over the top 200 unique parent candidates."""
    q = (q or "").strip()
    if not q:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="query 'q' must not be empty")
    if kind is not None and kind not in ("scrape", "crawl", "research", "corpus"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="kind must be one of: scrape, crawl, research, corpus")
    if mode not in retrieval.MODES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="mode must be one of: hybrid, semantic, keyword")
    try:
        result = await retrieval.facets(q, kind=kind, mode=mode)
    except retrieval.RetrievalUnavailable as e:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED,
                            detail=str(e)) from e
    return {"success": True, "query": q, "mode": mode, **result}


@app.get("/api/corpus")
async def browse_corpus(namespace: Optional[str] = None, framework: Optional[str] = None,
                        bucket: Optional[str] = None, tier: Optional[str] = None,
                        q: Optional[str] = None, target: Optional[str] = None,
                        offset: int = 0, limit: int = 50):
    """Browse the offline corpus (data/corpus/**/*.jsonl) with filters +
    pagination. Reads the JSONL files generically — the service never imports the
    corpus pipeline. Empty/absent corpus → an empty page, never an error."""
    if target is not None and target not in corpus_browser.TARGETS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"target must be one of: {', '.join(corpus_browser.TARGETS)}")
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    page = corpus_browser.browse(
        namespace=namespace, framework=framework, bucket=bucket, tier=tier,
        q=q, target=target, offset=offset, limit=limit)
    return {"success": True, **page}


@app.get("/api/corpus/stats")
async def corpus_stats():
    """Corpus record counts by target / namespace / bucket / tier, plus the
    distinct filter values, cached by the tree's file signature."""
    return {"success": True, "stats": corpus_browser.stats()}


@app.post("/api/map")
async def map_site(request: MapRequest):
    """Fast same-site URL discovery: sitemaps plus one shallow link pass over
    the base page. Returns links only — no page content is scraped."""
    if not request.url.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="URL must start with http:// or https://"
        )
    links = await sitemap.map_site(
        request.url, limit=request.limit,
        search=request.search, sitemap_only=request.sitemapOnly,
    )
    return {"success": True, "links": links, "count": len(links)}


@app.post("/api/batch/scrape", status_code=status.HTTP_202_ACCEPTED)
async def start_batch_scrape(request: BatchScrapeRequest, background_tasks: BackgroundTasks):
    """Scrape a fixed list of URLs as one asynchronous job; poll with the jobId."""
    bad = [u for u in request.urls if not u.startswith(("http://", "https://"))]
    if bad:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"URLs must start with http:// or https://: {bad[:5]}"
        )
    job_id = batcher.create_job(
        urls=request.urls,
        wait_for_ms=request.waitForMs,
        only_main_content=request.onlyMainContent,
        engine=request.engine,
    )
    background_tasks.add_task(batcher.run_batch, job_id)
    return {"success": True, "jobId": job_id}


@app.get("/api/batch/scrape/{job_id}")
async def get_batch_scrape_status(job_id: str):
    """Current state of a batch scrape: progress, per-page results, errors."""
    job = batcher.get_job(job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Batch job with ID '{job_id}' not found."
        )
    return job


@app.post("/api/research", status_code=status.HTTP_202_ACCEPTED)
async def start_research(request: ResearchRequest, background_tasks: BackgroundTasks):
    """Start an autonomous deep-research run; returns a jobId immediately."""
    if not extract_llm.configured():
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Research needs an LLM backend — set LOCAL_LLM_BASE_URL "
                   "(+ LOCAL_LLM_MODEL) or ANTHROPIC_API_KEY / AI_GATEWAY_API_KEY "
                   "in the service environment.")
    active = researcher.active_jobs()
    if len(active) >= research.MAX_CONCURRENT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "too many active research runs", "activeJobs": active})
    job_id = researcher.create_job(
        query=request.query, max_rounds=request.maxRounds,
        max_pages=request.maxPages, max_minutes=request.maxMinutes,
        seed_urls=request.seedUrls)
    background_tasks.add_task(researcher.run_research, job_id)
    return {"success": True, "jobId": job_id}


@app.get("/api/research")
async def list_research():
    """Summaries of all research runs in this process (newest first),
    without the report/activity/sources bodies — poll the per-job endpoint
    for those. snake_case fields, matching GET /api/research/{jobId}."""
    jobs = []
    for job in researcher.jobs.values():
        jobs.append({
            "job_id": job.get("job_id"),
            "query": job.get("query"),
            "status": job.get("status"),
            "rounds_run": job.get("rounds_run"),
            "pages_scraped": job.get("pages_scraped"),
            "llm_calls": job.get("llm_calls"),
            "sources_count": len(job.get("sources") or []),
            "insufficient": job.get("insufficient"),
            "start_time": job.get("start_time"),
            "end_time": job.get("end_time"),
            "artifact_stem": job.get("artifact_stem"),
        })
    jobs.sort(key=lambda j: j.get("start_time") or "9999", reverse=True)
    return {"jobs": jobs}


@app.get("/api/research/{job_id}")
async def get_research_status(job_id: str):
    """Current state of a research run: status, budgets, activity log, and the
    report + provenance once terminal."""
    job = researcher.get_job(job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Research job '{job_id}' not found.")
    return job


@app.post("/api/research/{job_id}/resume")
async def resume_research(job_id: str):
    """Resume a run that was interrupted by a restart (rehydrated from its
    checkpoint). Continues where it left off — no re-read pages, no
    double-counted budgets."""
    job = researcher.get_job(job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Research job '{job_id}' not found.")
    if job["status"] != "interrupted":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job is {job['status']}; only interrupted jobs can be resumed.")
    if not extract_llm.configured():
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Research needs an LLM backend — set LOCAL_LLM_BASE_URL "
                   "(+ LOCAL_LLM_MODEL) or ANTHROPIC_API_KEY / AI_GATEWAY_API_KEY "
                   "in the service environment.")
    active = researcher.active_jobs()
    if len(active) >= research.MAX_CONCURRENT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "too many active research runs", "activeJobs": active})
    researcher.resume_research(job_id)
    return {"success": True, "jobId": job_id, "status": job["status"]}


@app.post("/api/research/{job_id}/cancel")
async def cancel_research(job_id: str):
    """Request cancellation; the run winds down and synthesizes a partial report."""
    job = researcher.get_job(job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Research job '{job_id}' not found.")
    if not researcher.cancel(job_id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"Job is already {job['status']}.")
    return {"success": True, "jobId": job_id, "status": job["status"]}

@app.get("/api/artifacts")
async def list_artifacts():
    """List all saved scrape/crawl artifacts (newest first)."""
    return {"artifacts": storage.list_artifacts()}


@app.get("/artifacts", response_class=HTMLResponse)
async def artifacts_page():
    """Simple browsable index of saved artifacts."""
    items = storage.list_artifacts()
    rows = []
    for a in items:
        kb = f"{a['bytes'] / 1024:.1f} KB"
        when = datetime.datetime.fromtimestamp(a["mtime"]).strftime("%Y-%m-%d %H:%M")
        kind = a["kind"] if a["kind"] in {"scrape", "crawl", "research"} else "artifact"
        badge = html_lib.escape(str(a["kind"]))
        title = html_lib.escape(str(a["title"] or a["url"]))
        url = html_lib.escape(str(a["url"]))
        md_path = html_lib.escape(str(a["md"]), quote=True)
        json_path = html_lib.escape(str(a["json"]), quote=True)
        pages = (
            f" · {html_lib.escape(str(a['pages']))} pages"
            if a["kind"] != "scrape" else ""
        )
        rows.append(
            f"<tr>"
            f"<td><span class='badge {kind}'>{badge}</span></td>"
            f"<td><div class='title'>{title}</div>"
            f"<div class='url'>{url}</div></td>"
            f"<td class='meta'>{when}{pages}<br>{kb}</td>"
            f"<td class='links'><a href='{md_path}'>md</a> · <a href='{json_path}'>json</a></td>"
            f"</tr>"
        )
    table = "".join(rows) or "<tr><td colspan='4' class='empty'>No artifacts yet — run a scrape or crawl.</td></tr>"
    html = f"""<!doctype html><html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Scraped Artifacts</title><style>
body{{font-family:-apple-system,system-ui,Segoe UI,sans-serif;margin:0;background:#0f1115;color:#e6e6e6}}
.wrap{{max-width:900px;margin:0 auto;padding:32px 20px}}
h1{{font-size:22px;margin:0 0 4px}} .sub{{color:#8a8f98;margin:0 0 24px;font-size:14px}}
table{{width:100%;border-collapse:collapse}}
td{{padding:12px 10px;border-bottom:1px solid #232733;vertical-align:top;font-size:14px}}
.title{{font-weight:600}} .url{{color:#6b7280;font-size:12px;word-break:break-all}}
.meta{{color:#8a8f98;font-size:12px;white-space:nowrap}}
.links a{{color:#4a9eff;text-decoration:none}} .links a:hover{{text-decoration:underline}}
.badge{{font-size:11px;padding:2px 8px;border-radius:10px;font-weight:600}}
.badge.scrape{{background:#1e3a5f;color:#7cc4ff}} .badge.crawl{{background:#3a2f1e;color:#ffcf7c}}
.badge.research{{background:#1e3a2f;color:#7cffb0}}
.empty{{text-align:center;color:#6b7280;padding:40px}}
</style></head><body><div class='wrap'>
<h1>Scraped Artifacts</h1>
<p class='sub'>{len(items)} saved · auto-persisted to <code>data/</code> · newest first</p>
<table>{table}</table></div></body></html>"""
    return html


# Serve dashboard index.html at root
@app.get("/")
async def serve_dashboard():
    for dashboard_path in (
        "app/static/dashboard/index.html",
        "app/static/index.html",
    ):
        if os.path.exists(dashboard_path):
            return FileResponse(dashboard_path)
    return {"message": "Web scraper server running. Put index.html in app/static/ directory."}

# Mount static directory for JS/CSS files
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Mount the persisted artifacts directory so saved files are viewable in-browser
storage.ensure_dirs()
app.mount("/data", ArtifactStaticFiles(directory=storage.DATA_DIR), name="data")
