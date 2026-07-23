import asyncio
import datetime
import logging
import time
from typing import Dict, Any, List, Set, Optional
import uuid
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
from app.scraper import WebScraper
from app import changes, dedup, normalize, sitemap, storage, vecindex
from app.db import repo

logger = logging.getLogger("crawler")

TERMINAL = ("completed", "failed")
# Minimum seconds between checkpoint writes per job — bounds the IO of
# re-serializing the full job dict (results include page markdown) without
# giving up per-page durability on slow crawls.
CHECKPOINT_INTERVAL_S = 2.0


def _fresh_state(base_url: str) -> Dict[str, Any]:
    """Loop-carried crawl state, threaded through run_crawl so it can be
    checkpointed: the visited set, the pending (url, depth) frontier mirror
    of the asyncio.Queue, and the screenshot page counter."""
    return {"visited": set(), "pending": [(base_url, 0)], "shot_counter": 0}


class WebCrawler:
    """Legacy in-process crawler (compatibility only).

    Production crawls go through ``app.crawl`` (Postgres queue, leases,
    remote workers). This class remains for:

    * ``POST /api/llmstxt`` (bounded in-memory crawl + format)
    * ``GET /api/crawl/{id}`` / ``POST .../resume`` for pre-v0.4 checkpoints

    Do not add new product features here. New crawl work must use
    ``app.crawl.service.submit_crawl``. Import surface is fenced by
    ``tests/test_architecture_invariants.py``.
    """

    def __init__(self):
        # In-memory storage for crawl jobs
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self.scraper = WebScraper()
        # Strong refs to resumed background tasks (runner pattern) — resumes
        # are not tied to a request, so BackgroundTasks can't hold them.
        self._tasks: "set[asyncio.Task]" = set()
        # job_id -> loop-state payload from a restored checkpoint, consumed
        # when the job is resumed.
        self._pending_resume: Dict[str, Dict[str, Any]] = {}
        # job_id -> monotonic time of its last checkpoint write (throttle).
        self._last_ckpt: Dict[str, float] = {}

    def create_job(self, base_url: str, limit: int = 10, max_depth: int = 3, only_main_content: bool = True,
                   engine: str = "auto", use_sitemap: bool = True,
                   screenshots: bool = False) -> str:
        """Create a new crawl job and return its job ID."""
        job_id = str(uuid.uuid4())
        self.jobs[job_id] = {
            "job_id": job_id,
            "base_url": base_url,
            "status": "pending",
            "progress": 0.0,
            "processed_urls_count": 0,
            "limit": limit,
            "max_depth": max_depth,
            "only_main_content": only_main_content,
            "engine": engine,
            "use_sitemap": use_sitemap,
            "screenshots": screenshots,
            "sitemap_urls_found": 0,
            "results": [],
            "errors": [],
            "start_time": None,
            "end_time": None,
            "artifact_stem": None
        }
        return job_id

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Get the current state of a crawl job."""
        return self.jobs.get(job_id)

    def _checkpoint(self, job: Dict[str, Any], state: Dict[str, Any],
                    *, force: bool = False) -> None:
        """Persist the crawl for restart-survival; swallows its own failures.

        Synchronous on purpose: within the single event loop nothing can
        mutate job/state while this serializes, so no lock is needed. The
        throttle bounds the cost of rewriting the full job dict per page.
        """
        job_id = job["job_id"]
        now = time.monotonic()
        if not force and now - self._last_ckpt.get(job_id, 0.0) < CHECKPOINT_INTERVAL_S:
            return
        self._last_ckpt[job_id] = now
        try:
            storage.save_crawl_checkpoint(job_id, {
                "version": 1,
                "job": job,
                "loop": {
                    "visited": sorted(state["visited"]),
                    "pending": [[u, d] for u, d in state["pending"]],
                    "shot_counter": state["shot_counter"],
                },
                "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            })
        except Exception as e:
            logger.warning(f"crawl checkpoint failed for {job_id}: {e}")

    def restore_from_checkpoints(self) -> List[str]:
        """Rehydrate non-terminal crawl checkpoints into the job store
        (startup). Restored jobs get status "interrupted" and stay idle until
        resumed; their loop state is parked for resume_crawl. Returns the
        restored job ids, newest checkpoint first. Never raises."""
        restored: List[str] = []
        try:
            payloads = storage.load_crawl_checkpoints()
        except Exception as e:
            logger.error(f"failed to load crawl checkpoints: {e}")
            return restored
        for payload in payloads:
            job = payload.get("job") or {}
            job_id = job.get("job_id")
            if not job_id or job_id in self.jobs:
                continue
            if job.get("status") in TERMINAL:
                # Crash between save_crawl and checkpoint delete — done.
                storage.delete_crawl_checkpoint(job_id)
                continue
            job["status"] = "interrupted"
            self.jobs[job_id] = job
            self._pending_resume[job_id] = payload.get("loop") or {}
            logger.info(f"restored interrupted crawl {job_id} from checkpoint")
            restored.append(job_id)
        return restored

    def resume_crawl(self, job_id: str) -> bool:
        """Resume an interrupted crawl as a background task (runner pattern:
        asyncio.create_task + strong ref, not request-scoped BackgroundTasks)."""
        job = self.jobs.get(job_id)
        if not job or job["status"] != "interrupted":
            return False
        loop = self._pending_resume.pop(job_id, None) or {}
        state = _fresh_state(job["base_url"])
        state["visited"] = set(loop.get("visited") or [])
        state["pending"] = [(u, int(d)) for u, d in (loop.get("pending") or [])]
        state["shot_counter"] = int(loop.get("shot_counter") or 0)
        # Reconcile: URLs claimed by a worker but neither scraped nor errored
        # were in flight at the crash. Free their limit-budget slots (drop from
        # visited) WITHOUT re-queueing them — a page that crashes the process
        # must not crash-loop on resume; it only gets retried if a later page
        # re-discovers it via links.
        processed = {self._normalize_url(r.get("url", ""))
                     for r in job.get("results", [])}
        processed |= {self._normalize_url(e.get("url", ""))
                      for e in job.get("errors", [])}
        state["visited"] &= processed
        job["status"] = "pending"
        logger.info(f"resuming crawl {job_id} from checkpoint "
                    f"({len(state['visited'])} visited, {len(state['pending'])} pending)")
        task = asyncio.create_task(self.run_crawl(job_id, state=state))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def run_crawl(self, job_id: str, *, db_run_id: Optional[int] = None,
                        db_job_id: Optional[int] = None, db_trigger: str = "manual",
                        state: Optional[Dict[str, Any]] = None):
        """Asynchronously crawl a site within limits.

        db_run_id, when provided (job-definition / scheduled crawl), is the
        pre-created scrape_runs row this crawl reports against; otherwise an
        ad-hoc run is created here. All DB work is additive and swallowed — a
        persistence failure never changes the crawl result. A `state` from a
        restored checkpoint resumes mid-crawl: seeding is skipped and the
        frontier/visited set continue where the interrupted run stopped.
        """
        job = self.jobs.get(job_id)
        if not job:
            return

        resumed = state is not None
        job["status"] = "processing"
        job["start_time"] = (job.get("start_time")
                             or datetime.datetime.now(datetime.timezone.utc).isoformat())

        # Open (or adopt) the durable run record. external_id = crawler job_id so
        # GET /api/crawl/{jobId} stays correlatable with the DB row.
        try:
            if db_run_id is None:
                db_run_id = await repo.record_run_start(
                    external_id=job_id, job_id=db_job_id,
                    trigger=db_trigger, status="processing",
                )
            else:
                await repo.mark_run_processing(db_run_id, external_id=job_id)
        except Exception as e:
            logger.error(f"DB run-start failed for crawl {job_id}: {e}")
        
        base_url = job["base_url"]
        limit = job["limit"]
        max_depth = job["max_depth"]
        only_main_content = job["only_main_content"]
        engine = job.get("engine", "auto")
        
        parsed_base = urlparse(base_url)
        base_domain = parsed_base.netloc
        
        if state is None:
            state = _fresh_state(base_url)
        visited: Set[str] = state["visited"]
        queue = asyncio.Queue()

        # Seed the frontier from sitemaps (fuller coverage in fewer fetches
        # than pure link-walking); failures just mean link discovery only.
        # A resumed crawl skips seeding — its frontier is already materialized
        # in the checkpointed pending list.
        if not resumed and job.get("use_sitemap", True):
            try:
                seeded = await sitemap.discover(base_url, cap=limit * 3)
                job["sitemap_urls_found"] = len(seeded)
                base_norm = self._normalize_url(base_url)
                for u in seeded:
                    if self._normalize_url(u) != base_norm:
                        state["pending"].append((u, 1))
                logger.info(f"Seeded {len(seeded)} URLs from sitemaps for {base_url}")
            except Exception as e:
                logger.warning(f"Sitemap discovery failed for {base_url}: {e}")

        for pending_url, pending_depth in state["pending"]:
            await queue.put((pending_url, pending_depth))
        self._checkpoint(job, state, force=True)

        # Limit concurrency to 3 tasks to prevent flooding the server or getting blocked
        sem = asyncio.Semaphore(3)

        # Track pending task counts
        active_crawls = 0
        lock = asyncio.Lock()

        async def worker():
            nonlocal active_crawls
            while True:
                try:
                    # Get url and current depth
                    url, depth = await queue.get()
                except asyncio.CancelledError:
                    break
                
                normalized = self._normalize_url(url)

                # Check limits inside locks
                async with lock:
                    # Claim the URL: off the frontier mirror, into visited —
                    # so a crash mid-scrape skips it on resume rather than
                    # crash-looping on it.
                    try:
                        state["pending"].remove((url, depth))
                    except ValueError:
                        pass
                    if normalized in visited or len(visited) >= limit:
                        queue.task_done()
                        continue
                    visited.add(normalized)
                    active_crawls += 1
                
                # Run the scraping
                try:
                    async with sem:
                        logger.info(f"Crawling URL: {url} at depth {depth}")
                        scrape_result = await self.scraper.scrape(
                            url,
                            wait_for_ms=500,
                            only_main_content=only_main_content,
                            engine=engine
                        )
                    
                    # The private raw channel (verbatim html + screenshot bytes)
                    # never enters the job dict — only a saved screenshot's
                    # path does, and the disk write stays outside the job lock.
                    raw = scrape_result.pop("_raw", {}) or {}
                    screenshot_path = None
                    if (job.get("screenshots") and scrape_result["success"]
                            and raw.get("screenshot")):
                        async with lock:
                            state["shot_counter"] += 1
                            page_no = state["shot_counter"]
                        try:
                            paths = storage.save_run_raw(
                                job_id, page_no, screenshot=raw.get("screenshot"))
                            screenshot_path = paths.get("screenshot_path")
                        except Exception as e:
                            logger.warning(f"screenshot capture failed for {url}: {e}")

                    # Flag dup + change status before taking the job lock: dedup
                    # has its own thread lock, and change-tracking may await a
                    # DB fallback — neither belongs inside the workers' hot lock.
                    dedup_info = None
                    change_info = None
                    if scrape_result["success"]:
                        try:
                            dedup_info = dedup.check_and_register(scrape_result["markdown"], key=url)
                        except Exception as e:
                            logger.error(f"Dedup index update failed for {url}: {e}")
                        change_info = await changes.check_and_register(
                            url, (dedup_info or {}).get("content_hash"))

                    async with lock:
                        job["processed_urls_count"] = len(visited)
                        # Estimate progress based on visited URLs vs limit
                        job["progress"] = min(1.0, len(visited) / limit)

                        if scrape_result["success"]:
                            meta = scrape_result.get("metadata", {})
                            job["results"].append({
                                "url": url,
                                "title": scrape_result["title"],
                                "markdown": scrape_result["markdown"],
                                "description": scrape_result["description"],
                                "engine": meta.get("engine"),
                                "extractor": meta.get("extractor"),
                                "license": meta.get("license"),
                                "quality": meta.get("quality"),
                                "language": meta.get("language"),
                                "status_code": meta.get("status_code"),
                                "dedup": dedup_info,
                                "changeTracking": change_info,
                                "screenshot_path": screenshot_path
                            })
                            
                            # Extract and queue links if within depth limit
                            if depth < max_depth and len(visited) < limit:
                                links = self._extract_links(scrape_result["html"], base_url, base_domain)
                                for link in links:
                                    norm_link = self._normalize_url(link)
                                    if norm_link not in visited:
                                        state["pending"].append((link, depth + 1))
                                        await queue.put((link, depth + 1))
                        else:
                            job["errors"].append({
                                "url": url,
                                "error": scrape_result.get("error", "Unknown error occurred")
                            })
                except Exception as e:
                    async with lock:
                        job["errors"].append({
                            "url": url,
                            "error": f"Internal worker error: {str(e)}"
                        })
                finally:
                    async with lock:
                        active_crawls -= 1
                    # One durability point per page (success or error);
                    # throttled inside _checkpoint.
                    self._checkpoint(job, state)
                    queue.task_done()

        # Start 3 worker tasks in the background
        workers = [asyncio.create_task(worker()) for _ in range(3)]
        
        # Wait for the queue to become empty and all tasks to complete processing
        await queue.join()
        
        # Cancel the workers once work is done
        for w in workers:
            w.cancel()
        
        # Wait for workers to shut down
        await asyncio.gather(*workers, return_exceptions=True)
        
        # Mark job status
        async with lock:
            job["status"] = "completed" if len(job["results"]) > 0 else "failed"
            job["progress"] = 1.0
            job["end_time"] = datetime.datetime.now(datetime.timezone.utc).isoformat()

        # Persist the finished crawl so results survive restarts/redeploys,
        # then drop the checkpoint — the artifact is now the durable record.
        stem = None
        try:
            stem = storage.save_crawl(job)
            job["artifact_stem"] = stem
            storage.delete_crawl_checkpoint(job_id)
        except Exception as e:
            logger.error(f"Failed to save crawl artifact: {e}")

        # Best-effort semantic index of each crawled page (no-op unless
        # EMBEDDINGS_BASE_URL is set). One ref per page so hits link to the page,
        # not the whole crawl; swallow-and-default keeps a failure invisible.
        if stem:
            for idx, item in enumerate(job.get("results", []) or []):
                await vecindex.index_document(
                    "crawl", f"{stem}#{idx}", item.get("url"),
                    item.get("markdown", ""),
                    meta={"title": item.get("title"), "url": item.get("url"),
                          "crawl": stem})

        # Record pages + finalize the run in the database (additive; swallowed).
        # Done here at the end rather than per-page so the 3-worker hot loop and
        # its asyncio.Lock are never blocked on a DB round-trip.
        if db_run_id is not None:
            try:
                for item in job.get("results", []):
                    await repo.record_page(db_run_id, **normalize.page_row_from_crawl_item(
                        item, screenshot_path=item.get("screenshot_path")))
                for err in job.get("errors", []):
                    await repo.record_error(
                        db_run_id, page_url=err.get("url"),
                        stage="crawl", message=err.get("error"),
                    )
                await repo.record_run_finish(
                    db_run_id,
                    status=job["status"],
                    engine_used=job.get("engine"),
                    pages_count=len(job.get("results", [])),
                    raw_output_path=(f"data/crawls/{stem}.json" if stem else None),
                )
            except Exception as e:
                logger.error(f"DB run-finalize failed for crawl {job_id}: {e}")

    def _normalize_url(self, url: str) -> str:
        """Standardize URLs to avoid crawling identical pages.

        Delegates to app.normalize so the crawl frontier and the persistence
        layer share one definition of URL identity.
        """
        return normalize.normalize_url(url)

    def _extract_links(self, html: str, base_url: str, base_domain: str) -> List[str]:
        """Extract valid absolute links on the same domain or subdomain."""
        soup = BeautifulSoup(html, "html.parser")
        links = []
        
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
                
            full_url = urljoin(base_url, href)
            parsed_url = urlparse(full_url)
            
            # Match domain or subdomains
            target_domain = parsed_url.netloc.lower()
            ref_domain = base_domain.lower()
            
            if target_domain == ref_domain or target_domain.endswith("." + ref_domain):
                # Filter out obvious static binary file extensions
                path = parsed_url.path.lower()
                ignore_extensions = [
                    ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".tar", ".gz", 
                    ".mp3", ".mp4", ".css", ".js", ".svg", ".ico", ".woff", ".woff2"
                ]
                if not any(path.endswith(ext) for ext in ignore_extensions):
                    links.append(full_url)
                    
        return list(set(links))
