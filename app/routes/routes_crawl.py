from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status

from app.crawl import repository
from app.crawl.config import CrawlConfig
from app.crawl.repository import PersistenceUnavailable
from app.crawl.service import crawl_service
from app.services import crawler
from app.url_safety import UnsafeUrlError


router = APIRouter(prefix="/api/crawl", tags=["crawl"])


def _unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "persistence_unavailable",
            "message": "Durable crawling requires PostgreSQL",
        },
    )


def _job_response(job: dict) -> dict:
    response = {key: value for key, value in job.items() if key != "results"}
    response["status"] = job.get("state")
    response["resultCount"] = len(job.get("results") or [])
    response["results"] = []
    response["job_id"] = str(job.get("id"))
    response["processed_urls_count"] = job.get("terminal_count", 0)
    discovered = max(1, job.get("discovered_count", 0))
    response["progress"] = min(1.0, job.get("terminal_count", 0) / discovered)
    return response


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def submit(config: CrawlConfig):
    if not config.url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")
    try:
        job_id = await crawl_service.submit_crawl(config)
    except UnsafeUrlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PersistenceUnavailable as exc:
        raise _unavailable() from exc
    return {"success": True, "jobId": str(job_id)}


@router.get("/{job_id}")
async def get_status(job_id: str):
    persistence_error = None
    try:
        durable_id = UUID(job_id)
    except ValueError:
        durable_id = None
    if durable_id is not None:
        try:
            job = await repository.get_job(durable_id)
        except PersistenceUnavailable as exc:
            persistence_error = exc
        else:
            if job is not None:
                return _job_response(job)
    legacy = crawler.get_job(job_id)
    if legacy is not None:
        return legacy
    if persistence_error is not None:
        raise _unavailable() from persistence_error
    raise HTTPException(status_code=404, detail="Crawl job not found")


@router.get("/{job_id}/pages")
async def get_pages(job_id: UUID, after: int = Query(-1, ge=-1),
                    limit: int = Query(50, ge=1, le=100)):
    try:
        pages = await repository.list_pages(job_id, after, limit)
    except PersistenceUnavailable as exc:
        raise _unavailable() from exc
    if pages is None:
        raise HTTPException(status_code=404, detail="Crawl job not found")
    next_after = pages[-1].get("discovery_seq", after) if pages else after
    return {"pages": pages, "nextAfter": next_after}


@router.post("/{job_id}/cancel")
async def cancel(job_id: UUID):
    try:
        existing = await repository.get_job(job_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Crawl job not found")
        if existing.get("state") in {
            "completed", "partial", "failed", "timed_out",
        }:
            raise HTTPException(status_code=409, detail="Crawl job is already terminal")
        if not await repository.request_cancel(job_id):
            raise HTTPException(status_code=404, detail="Crawl job not found")
        job = await repository.get_job(job_id)
    except PersistenceUnavailable as exc:
        raise _unavailable() from exc
    return {"success": True, "jobId": str(job_id), "status": job.get("state")}


@router.post("/{job_id}/resume")
async def resume(job_id: str):
    try:
        durable_id = UUID(job_id)
    except ValueError:
        durable_id = None
    if durable_id is not None:
        try:
            if await repository.get_job(durable_id) is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Durable crawl jobs recover automatically",
                )
        except PersistenceUnavailable:
            pass
    legacy = crawler.get_job(job_id)
    if not legacy:
        raise HTTPException(status_code=404, detail="Crawl job not found")
    if legacy.get("status") != "interrupted" or not crawler.resume_crawl(job_id):
        raise HTTPException(status_code=409, detail="Only restored interrupted crawls can resume")
    return {"success": True, "jobId": job_id, "status": "pending"}
