"""Batch scrape: a fixed list of URLs run as one async job.

Mirrors the crawler's in-memory job pattern (jobId + status polling; a restart
loses in-flight batches) but needs no frontier — the URL list is fixed, so the
workers are just a gather bounded by a semaphore. Each page runs the normal
scrape pipeline (dedup + change-tracking flags, file artifact, raw capture).

The whole batch is indexed as ONE scrape_runs row (external_id = the batch
jobId) with one scraped_pages row per URL, recorded after the hot loop like
the crawler does. All DB work is additive and swallowed.
"""
import asyncio
import datetime
import logging
import uuid
from typing import Any, Dict, List, Optional

from app import changes, dedup, normalize, storage
from app.db import repo
from app.scraper import WebScraper

logger = logging.getLogger("batch")

CONCURRENCY = 3
MAX_URLS = 50


class BatchManager:
    def __init__(self):
        # In-memory storage for batch jobs
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self.scraper = WebScraper()

    def create_job(self, urls: List[str], wait_for_ms: int = 1000,
                   only_main_content: bool = True, engine: str = "auto") -> str:
        job_id = str(uuid.uuid4())
        self.jobs[job_id] = {
            "job_id": job_id,
            "urls": list(urls),
            "status": "pending",
            "progress": 0.0,
            "total": len(urls),
            "completed": 0,
            "wait_for_ms": wait_for_ms,
            "only_main_content": only_main_content,
            "engine": engine,
            "results": [],
            "errors": [],
            "start_time": None,
            "end_time": None,
        }
        return job_id

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        return self.jobs.get(job_id)

    async def run_batch(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if not job:
            return
        job["status"] = "processing"
        job["start_time"] = datetime.datetime.now(datetime.timezone.utc).isoformat()

        db_run_id = None
        try:
            db_run_id = await repo.record_run_start(
                external_id=job_id, trigger="batch", status="processing")
        except Exception as e:
            logger.error(f"DB run-start failed for batch {job_id}: {e}")

        sem = asyncio.Semaphore(CONCURRENCY)
        # (item, page_row) pairs collected for the post-loop DB pass
        page_rows: List[Dict[str, Any]] = []

        async def one(url: str) -> None:
            try:
                async with sem:
                    result = await self.scraper.scrape(
                        url,
                        wait_for_ms=job["wait_for_ms"],
                        only_main_content=job["only_main_content"],
                        engine=job["engine"],
                    )
            except Exception as e:
                job["errors"].append({"url": url, "error": f"Internal error: {e}"})
                job["completed"] += 1
                job["progress"] = job["completed"] / job["total"]
                return

            raw = result.pop("_raw", {}) or {}
            if not result.get("success"):
                job["errors"].append(
                    {"url": url, "error": result.get("error", "Unknown error")})
            else:
                try:
                    result["metadata"]["dedup"] = dedup.check_and_register(
                        result["markdown"], key=url)
                except Exception as e:
                    logger.error(f"dedup failed for {url}: {e}")
                change_info = await changes.check_and_register(
                    url, (result["metadata"].get("dedup") or {}).get("content_hash"))
                if change_info is not None:
                    result["metadata"]["changeTracking"] = change_info
                stem = None
                try:
                    stem = storage.save_scrape(result)
                except Exception as e:
                    logger.error(f"save_scrape failed for {url}: {e}")
                raw_paths: Dict[str, str] = {}
                try:
                    raw_paths = storage.save_run_raw(
                        stem, 1, raw_html=raw.get("html"),
                        screenshot=raw.get("screenshot"))
                except Exception as e:
                    logger.error(f"raw capture failed for {url}: {e}")
                result["artifact"] = f"data/scrapes/{stem}.json" if stem else None
                job["results"].append(result)
                page_rows.append(normalize.page_row_from_result(
                    result, stem,
                    raw_html_path=raw_paths.get("raw_html_path"),
                    screenshot_path=raw_paths.get("screenshot_path")))
            job["completed"] += 1
            job["progress"] = job["completed"] / job["total"]

        await asyncio.gather(*(one(u) for u in job["urls"]))

        job["status"] = "completed" if job["results"] else "failed"
        job["progress"] = 1.0
        job["end_time"] = datetime.datetime.now(datetime.timezone.utc).isoformat()

        # Index pages + finalize the run after the hot loop (crawler pattern);
        # additive and swallowed — a DB failure never changes the job result.
        if db_run_id is not None:
            try:
                for row in page_rows:
                    await repo.record_page(db_run_id, **row)
                for err in job["errors"]:
                    await repo.record_error(
                        db_run_id, page_url=err.get("url"),
                        stage="batch", message=err.get("error"))
                await repo.record_run_finish(
                    db_run_id, status=job["status"],
                    engine_used=job.get("engine"),
                    pages_count=len(job["results"]))
            except Exception as e:
                logger.error(f"DB run-finalize failed for batch {job_id}: {e}")
