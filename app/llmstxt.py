"""llms.txt generation from a crawl.

llms.txt is a compact, LLM-ready site index: a site title heading plus one
`- [Page Title](url): description` line per page. llms-full.txt is the
long-form companion: every page's markdown, concatenated. Both are generated
from a finished crawl's results — POST /api/llmstxt runs a bounded fresh
**legacy** ``WebCrawler`` job (202 + jobId polling) and then formats.

This path intentionally stays on the in-memory crawler so it works without
Postgres. Durable site crawls use ``POST /api/crawl`` instead.

generate() is pure (results in, text out) so the format is testable without
a crawl; run() is the background-task glue.
"""
import logging
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse

from app import storage

logger = logging.getLogger("llmstxt")


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


async def run(crawler, job_id: str) -> None:
    """Background task: crawl, then attach the generated files to the job.

    Failures after a successful crawl are recorded on the job (so the poll
    endpoint can report them) but never raise — same contract as the crawl
    itself.
    """
    await crawler.run_crawl(job_id)
    job = crawler.get_job(job_id)
    if not job:
        return
    try:
        txt, full = generate(job["base_url"], job.get("results") or [])
        job["llmstxt"] = storage.save_llmstxt(job["base_url"], txt, full)
    except Exception as e:
        logger.error("llms.txt generation failed for %s: %s", job_id, e)
        job["llmstxt_error"] = str(e)
