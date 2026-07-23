"""llms.txt generation from a durable crawl.

llms.txt is a compact, LLM-ready site index: a site title heading plus one
`- [Page Title](url): description` line per page. llms-full.txt is the
long-form companion: every page's markdown, concatenated.

POST /api/llmstxt submits a bounded durable crawl (Postgres queue), then a
background task waits for the job to finish and formats the results. Poll
GET /api/llmstxt/{jobId}. Requires DATABASE_URL, same as POST /api/crawl.

generate() is pure (results in, text out) so the format is testable without
a crawl; run_for_job() is the background-task glue.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse
from uuid import UUID

from app import storage
from app.crawl import repository
from app.crawl.repository import PersistenceUnavailable

logger = logging.getLogger("llmstxt")

_TERMINAL_JOB_STATES = frozenset({
    "completed", "partial", "failed", "cancelled", "timed_out",
})
_POLL_SECONDS = 0.5
_MAX_WAIT_SECONDS = 6 * 60 * 60  # match default crawl timeout ceiling


def _clean(text: str) -> str:
    """Single-line, link-safe cell text."""
    return " ".join((text or "").split())


def generate(base_url: str, results: List[Dict[str, Any]]) -> Tuple[str, str]:
    """Format crawl results as (llms_txt, llms_full_txt)."""
    host = urlparse(base_url).netloc or base_url
    site_title = _clean(results[0].get("title") if results else "") or host

    lines = [f"# {site_title}", ""]
    for page in results:
        title = _clean(page.get("title")) or page.get("url", "")
        desc = _clean(page.get("description"))
        entry = f"- [{title}]({page.get('url', '')})"
        lines.append(f"{entry}: {desc}" if desc else entry)
    llms_txt = "\n".join(lines) + "\n"

    full_parts = []
    for page in results:
        title = _clean(page.get("title")) or page.get("url", "")
        full_parts.append(f"# {title}\n{page.get('url', '')}\n\n"
                          f"{page.get('markdown') or ''}")
    llms_full_txt = "\n\n---\n\n".join(full_parts) + ("\n" if full_parts else "")
    return llms_txt, llms_full_txt


def pages_from_durable_job(job: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Map durable crawl_results rows to the generate() page shape."""
    pages: List[Dict[str, Any]] = []
    for row in job.get("results") or []:
        meta = row.get("metadata") or {}
        if not isinstance(meta, dict):
            meta = {}
        pages.append({
            "url": row.get("final_url") or "",
            "title": row.get("title") or meta.get("title") or "",
            "description": meta.get("description") or "",
            "markdown": row.get("markdown") or "",
        })
    return pages


def base_url_from_job(job: Dict[str, Any]) -> str:
    config = job.get("config") or {}
    if isinstance(config, dict) and config.get("url"):
        return str(config["url"])
    results = job.get("results") or []
    if results and results[0].get("final_url"):
        return str(results[0]["final_url"])
    return ""


async def wait_for_terminal_job(
    job_id: UUID,
    *,
    poll_seconds: float = _POLL_SECONDS,
    max_wait_seconds: float = _MAX_WAIT_SECONDS,
) -> Optional[Dict[str, Any]]:
    """Poll durable job status until terminal or timeout/unavailable."""
    elapsed = 0.0
    while elapsed <= max_wait_seconds:
        try:
            job = await repository.get_job(job_id)
        except PersistenceUnavailable:
            logger.error("llmstxt wait aborted: persistence unavailable job_id=%s", job_id)
            return None
        if job is None:
            return None
        if job.get("state") in _TERMINAL_JOB_STATES:
            return job
        await asyncio.sleep(poll_seconds)
        elapsed += poll_seconds
    logger.error("llmstxt wait timed out job_id=%s after %.0fs", job_id, max_wait_seconds)
    return None


async def run_for_job(job_id: UUID) -> None:
    """Background task: wait for durable crawl, then write llms.txt artifacts.

    Failures after a successful crawl are recorded on the job status sidecar
    (so the poll endpoint can report them) but never raise.
    """
    job_key = str(job_id)
    job = await wait_for_terminal_job(job_id)
    if job is None:
        try:
            storage.save_llmstxt_job_status(job_key, {
                "status": "error",
                "error": "crawl job not found or wait timed out",
            })
        except Exception as e:
            logger.error("llmstxt status write failed for %s: %s", job_key, e)
        return
    try:
        base_url = base_url_from_job(job)
        txt, full = generate(base_url, pages_from_durable_job(job))
        paths = storage.save_llmstxt(base_url, txt, full)
        storage.save_llmstxt_job_status(job_key, {
            "status": "ready",
            "paths": paths,
            "pages": len(job.get("results") or []),
            "crawl_state": job.get("state"),
        })
    except Exception as e:
        logger.error("llms.txt generation failed for %s: %s", job_key, e)
        try:
            storage.save_llmstxt_job_status(job_key, {
                "status": "error",
                "error": str(e)[:500],
                "crawl_state": job.get("state"),
            })
        except Exception as write_err:
            logger.error("llmstxt status write failed for %s: %s", job_key, write_err)


def status_payload(job: Dict[str, Any], job_id: str) -> Dict[str, Any]:
    """Build the GET /api/llmstxt/{id} response body from a durable job."""
    discovered = max(1, int(job.get("discovered_count") or 0))
    terminal = int(job.get("terminal_count") or 0)
    state = job.get("state") or "pending"
    out: Dict[str, Any] = {
        "success": True,
        "jobId": job_id,
        "status": state,
        "progress": min(1.0, terminal / discovered),
        "pagesProcessed": int(job.get("succeeded_count") or 0),
    }
    sidecar = storage.load_llmstxt_job_status(job_id) or {}
    if sidecar.get("status") == "error" and sidecar.get("error"):
        out["error"] = sidecar["error"]
    paths = sidecar.get("paths") if isinstance(sidecar.get("paths"), dict) else None
    if paths:
        out["llmstxtPath"] = paths.get("llmstxt_path")
        out["llmsFullPath"] = paths.get("llms_full_path")
        try:
            import os
            with open(
                os.path.join(
                    storage.LLMSTXT_DIR,
                    os.path.basename(paths["llmstxt_path"]),
                ),
                encoding="utf-8",
            ) as f:
                out["llmstxt"] = f.read()
        except Exception as e:
            logger.warning("could not read llms.txt for %s: %s", job_id, e)
    elif state in _TERMINAL_JOB_STATES and sidecar.get("status") != "error":
        # Crawl finished; generation still running or not yet started.
        out["status"] = "formatting"
    return out
