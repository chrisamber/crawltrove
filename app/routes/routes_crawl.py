import asyncio
import json
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.crawl import repository
from app.crawl.config import CrawlConfig
from app.crawl.repository import PersistenceUnavailable
from app.crawl.service import ProviderBudgetInvalid, crawl_service
from app.acquisition.registry import ProviderUnavailable
from app.services import crawler
from app.url_safety import UnsafeUrlError
from app.acquisition import sessions


router = APIRouter(prefix="/api/crawl", tags=["crawl"])


class RetryFailuresRequest(BaseModel):
    taskIds: list[UUID] | None = Field(default=None, min_length=1, max_length=100)


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
    except ProviderUnavailable as exc:
        raise HTTPException(status_code=503, detail={
            "code": "provider_unavailable", "message": "Requested provider is unavailable",
        }) from exc
    except ProviderBudgetInvalid as exc:
        raise HTTPException(status_code=422, detail={
            "code": "provider_budget_invalid", "message": str(exc),
        }) from exc
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
            "completed", "partial", "failed", "cancelled", "timed_out",
        }:
            raise HTTPException(status_code=409, detail="Crawl job is already terminal")
        if not await repository.request_cancel(job_id):
            raise HTTPException(status_code=404, detail="Crawl job not found")
        job = await repository.get_job(job_id)
    except PersistenceUnavailable as exc:
        raise _unavailable() from exc
    return {"success": True, "jobId": str(job_id), "status": job.get("state")}


@router.post("/{job_id}/retry-failures", status_code=status.HTTP_202_ACCEPTED)
async def retry_failures(job_id: UUID, request: RetryFailuresRequest | None = None):
    try:
        retry_job = await repository.retry_failures(
            job_id, tuple(request.taskIds or ()) if request is not None else (),
        )
    except PersistenceUnavailable as exc:
        raise _unavailable() from exc
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Crawl job not found") from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    crawl_service.wake()
    return {"success": True, "jobId": str(retry_job), "sourceJobId": str(job_id)}


@router.get("/{job_id}/events")
async def events(
    job_id: UUID, request: Request,
    after: int = Query(0, ge=0, le=9_223_372_036_854_775_807),
):
    last_header = request.headers.get("last-event-id")
    if last_header:
        try:
            header_cursor = int(last_header)
            if not 0 <= header_cursor <= 9_223_372_036_854_775_807:
                raise ValueError
            after = max(after, header_cursor)
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="Last-Event-ID must be a non-negative 64-bit integer",
            ) from exc
    try:
        if await repository.list_events(job_id, after, 1) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Crawl job not found")
    except PersistenceUnavailable as exc:
        raise _unavailable() from exc

    async def stream():
        cursor = after
        while not await request.is_disconnected():
            rows = await repository.list_events(job_id, cursor, 100) or []
            for row in rows:
                cursor = row["id"]
                payload = json.dumps({
                    "taskId": str(row["task_id"]) if row["task_id"] else None,
                    "event": row["event"], "metadata": row["metadata"],
                    "createdAt": row["created_at"].isoformat(),
                }, separators=(",", ":"))
                yield f"id: {cursor}\nevent: {row['event']}\ndata: {payload}\n\n"
            state = await repository.job_state(job_id)
            if not rows and state in {
                "completed", "partial", "failed", "cancelled", "timed_out",
            }:
                return
            if not rows:
                yield ": keepalive\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(
        stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{job_id}/sessions/{session_id}/token")
async def session_token(job_id: UUID, session_id: UUID):
    try:
        if not await sessions.belongs_to_job(session_id, job_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Session not found")
        token = await sessions.issue_token(session_id, "control", ttl_seconds=60)
    except sessions.SessionPersistenceUnavailable as exc:
        raise _unavailable() from exc
    except sessions.SessionStateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return {"url": f"/api/acquisition/sessions/{session_id}/open?token={token}"}


@router.post("/{job_id}/sessions/{session_id}/resume", status_code=status.HTTP_202_ACCEPTED)
async def session_resume(job_id: UUID, session_id: UUID):
    try:
        if not await sessions.belongs_to_job(session_id, job_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Session not found")
        if not await sessions.request_resume(session_id):
            raise HTTPException(status.HTTP_409_CONFLICT, detail="Session is not resumable")
    except sessions.SessionPersistenceUnavailable as exc:
        raise _unavailable() from exc
    return {"status": "resuming"}


@router.post("/{job_id}/sessions/{session_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def session_cancel(job_id: UUID, session_id: UUID):
    try:
        if not await sessions.belongs_to_job(session_id, job_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Session not found")
        if not await sessions.cancel(session_id):
            raise HTTPException(status.HTTP_409_CONFLICT, detail="Session is already closed")
    except sessions.SessionPersistenceUnavailable as exc:
        raise _unavailable() from exc
    return {"status": "cancelled"}


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
