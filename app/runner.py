"""Execution of scrape_jobs definitions, shared by the /run endpoint and scheduler.

A job definition (scrape_jobs row) is turned into one scrape_runs execution:
  * launch_job() creates the run row immediately (so the HTTP endpoint can return
    {runId} at once) and dispatches the work as a background task.
  * scrape-kind work runs inline here; crawl-kind work is delegated to the shared
    crawler, which records its own pages against the same run_id.

Everything DB-side is additive and swallowed — a persistence failure must never
break execution. Files on disk remain the source of truth.
"""
import asyncio
import logging
from typing import Any, Dict, Optional

from app import changes, dedup, normalize, storage, vecindex
from app.crawl import repository as crawl_repository
from app.crawl.config import CrawlConfig
from app.crawl.service import submit_crawl
from app.db import repo
from app.services import scraper

logger = logging.getLogger("runner")

# Keep strong refs to in-flight background tasks so they are not GC'd mid-run.
_tasks: "set[asyncio.Task]" = set()


def page_fields_from_scrape(
    result: Dict[str, Any], stem: Optional[str], **raw,
) -> Dict[str, Any]:
    """Map a scrape result + storage stem to scraped_pages columns.

    Thin wrapper kept for existing call sites; delegates to the shared mapper in
    app.normalize. Raw-capture kwargs (raw_html_path, screenshot_path) pass through.
    """
    return normalize.page_row_from_result(result, stem, **raw)


async def persist_scrape_page(
    scraped: Dict[str, Any],
    raw: Optional[Dict[str, Any]] = None,
    *,
    trigger: str = "manual",
) -> Dict[str, Optional[int]]:
    """Shared scrape persistence for the /scrape and /extract HTTP paths.

    Flags dups (mutating scraped["metadata"]["dedup"] in place so the caller's
    response carries it), saves the file artifact + raw capture, opens a run, and
    records one page. Returns {run_id, page_id, stem}; the caller finishes the run
    (and, for /extract, inserts records first). Every DB/disk step is swallowed —
    a failure never changes the HTTP response.
    """
    raw = raw or {}
    try:
        scraped["metadata"]["dedup"] = dedup.check_and_register(
            scraped["markdown"], key=scraped["url"])
    except Exception as e:
        logger.error(f"dedup failed for {scraped.get('url')}: {e}")
    change_info = await changes.check_and_register(
        scraped.get("url"),
        (scraped["metadata"].get("dedup") or {}).get("content_hash"))
    if change_info is not None:
        scraped["metadata"]["changeTracking"] = change_info
    stem = None
    try:
        stem = storage.save_scrape(scraped)
    except Exception as e:
        logger.error(f"save_scrape failed for {scraped.get('url')}: {e}")
    raw_paths: Dict[str, str] = {}
    try:
        raw_paths = storage.save_run_raw(
            stem, 1, raw_html=raw.get("html"), screenshot=raw.get("screenshot"))
    except Exception as e:
        logger.error(f"raw capture failed for {scraped.get('url')}: {e}")
    # Best-effort semantic index (no-op unless EMBEDDINGS_BASE_URL is set;
    # swallow-and-default so an embedding failure never changes the response).
    if stem:
        await vecindex.index_document(
            "scrape", stem, scraped.get("url"), scraped.get("markdown", ""),
            meta={"title": scraped.get("title"), "url": scraped.get("url")})
    run_id = page_id = None
    try:
        run_id = await repo.record_run_start(
            external_id=stem, trigger=trigger, status="processing")
        if run_id is not None:
            page_id = await repo.record_page(run_id, **normalize.page_row_from_result(
                scraped, stem,
                raw_html_path=raw_paths.get("raw_html_path"),
                screenshot_path=raw_paths.get("screenshot_path")))
            # Mirror any swallowed corpus-signal failures into scrape_errors so
            # they're queryable (stage='signal:<name>'), without failing anything.
            for se in (scraped.get("metadata", {}).get("signal_errors") or []):
                await repo.record_error(
                    run_id, page_url=scraped.get("url"),
                    stage=f"signal:{se.get('signal')}", message=se.get("message"))
    except Exception as e:
        logger.error(f"record page failed for {scraped.get('url')}: {e}")
    return {"run_id": run_id, "page_id": page_id, "stem": stem}


def _scrape_params(params: Dict[str, Any]) -> Dict[str, Any]:
    params = params or {}
    return {
        "wait_for_ms": params.get("wait_for_ms", params.get("waitForMs", 1000)),
        "only_main_content": params.get("only_main_content", params.get("onlyMainContent", True)),
        "engine": params.get("engine", "auto"),
    }


def _crawl_params(params: Dict[str, Any]) -> Dict[str, Any]:
    params = params or {}
    return {
        "limit": params.get("limit", 10),
        "max_depth": params.get("max_depth", params.get("maxDepth", 3)),
        "only_main_content": params.get("only_main_content", params.get("onlyMainContent", True)),
        "engine": params.get("engine", "auto"),
        "use_sitemap": params.get("use_sitemap", params.get("useSitemap", True)),
    }


def _crawl_config(params: Dict[str, Any]) -> Dict[str, Any]:
    values = _crawl_params(params)
    return {
        "limit": values["limit"],
        "maxDepth": values["max_depth"],
        "onlyMainContent": values["only_main_content"],
        "engine": values["engine"],
        "useSitemap": values["use_sitemap"],
        "screenshots": params.get("screenshots", False),
    }


async def launch_job(def_row: Dict[str, Any], trigger: str = "manual") -> Optional[int]:
    """Create a run for a job definition and dispatch it. Returns the run id."""
    if (def_row.get("kind") or "scrape").lower() == "crawl":
        target_url = def_row.get("target_url")
        if not target_url:
            return None
        config = CrawlConfig.model_validate({
            "url": target_url, **_crawl_config(def_row.get("params") or {}),
        })
        crawl_id = await submit_crawl(
            config, definition_job_id=def_row.get("id"), trigger=trigger,
        )
        durable = await crawl_repository.get_job(crawl_id)
        return durable.get("run_id") if durable else None
    run_id = await repo.record_run_start(
        job_id=def_row.get("id"), trigger=trigger, status="pending"
    )
    if run_id is None:
        return None
    task = asyncio.create_task(_execute(def_row, run_id, trigger))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return run_id


async def _execute(def_row: Dict[str, Any], run_id: int, trigger: str) -> None:
    kind = (def_row.get("kind") or "scrape").lower()
    target_url = def_row.get("target_url")
    params = def_row.get("params") or {}
    try:
        if not target_url:
            await repo.record_error(run_id, page_url=None, stage="runner",
                                    message="job has no target_url")
            await repo.record_run_finish(run_id, status="failed",
                                         error_message="job has no target_url")
            return

        # scrape kind
        await repo.mark_run_processing(run_id)
        result = await scraper.scrape(url=target_url, **_scrape_params(params))
        if not result.get("success"):
            err = result.get("error", "scrape failed")
            await repo.record_error(run_id, page_url=target_url, stage="scrape", message=err)
            await repo.record_run_finish(run_id, status="failed", error_message=err)
            return

        # Mirror the ad-hoc scrape path: strip raw channel, flag dups, persist
        # file artifact + raw capture, then index.
        raw = result.pop("_raw", {}) or {}
        try:
            result["metadata"]["dedup"] = dedup.check_and_register(
                result["markdown"], key=result["url"])
        except Exception as e:
            logger.error(f"dedup failed for {target_url}: {e}")
        change_info = await changes.check_and_register(
            result.get("url"),
            (result["metadata"].get("dedup") or {}).get("content_hash"))
        if change_info is not None:
            result["metadata"]["changeTracking"] = change_info
        stem = None
        try:
            stem = storage.save_scrape(result)
        except Exception as e:
            logger.error(f"save_scrape failed for {target_url}: {e}")
        raw_paths = {}
        try:
            raw_paths = storage.save_run_raw(
                stem, 1, raw_html=raw.get("html"), screenshot=raw.get("screenshot"))
        except Exception as e:
            logger.error(f"raw capture failed for {target_url}: {e}")
        if stem:
            await vecindex.index_document(
                "scrape", stem, result.get("url"), result.get("markdown", ""),
                meta={"title": result.get("title"), "url": result.get("url")})

        await repo.record_page(run_id, **page_fields_from_scrape(
            result, stem,
            raw_html_path=raw_paths.get("raw_html_path"),
            screenshot_path=raw_paths.get("screenshot_path")))
        await repo.record_run_finish(
            run_id,
            status="completed",
            engine_used=result.get("metadata", {}).get("engine"),
            pages_count=1,
            raw_output_path=(f"data/scrapes/{stem}.json" if stem else None),
            external_id=stem,
        )
    except Exception as e:
        logger.error(f"job execution failed (run {run_id}): {e}")
        await repo.record_error(run_id, page_url=target_url, stage="runner", message=str(e))
        await repo.record_run_finish(run_id, status="failed", error_message=str(e))
