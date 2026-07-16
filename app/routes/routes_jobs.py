"""REST: recurring/named scrape job definitions (scrape_jobs).

Endpoints (camelCase JSON, snake_case Python via populate_by_name):
    POST /api/jobs            create a job definition
    GET  /api/jobs            list job definitions
    GET  /api/jobs/{id}       fetch one
    POST /api/jobs/{id}/run   fire it now -> 202 {runId}

All require persistence (DATABASE_URL). When disabled they return 503 so the
behaviour is explicit rather than silently doing nothing.
"""
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.db import pool, repo
from app import runner
from app.routes.serialize import job_to_api

router = APIRouter(prefix="/api", tags=["jobs"])


def _require_db() -> None:
    if not pool.enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Persistence is not configured — set DATABASE_URL to use jobs.",
        )


class JobCreate(BaseModel):
    name: Optional[str] = Field(None, description="Human label for the job")
    kind: str = Field("scrape", pattern="^(scrape|crawl)$")
    targetUrl: str = Field(..., alias="targetUrl", description="URL to scrape/crawl")
    params: Dict[str, Any] = Field(default_factory=dict, description="Per-run options (engine, limit, …)")
    schedule: Optional[str] = Field(
        None, description="Interval spec: '15m', '2h', '@daily', or null for manual-only"
    )
    enabled: bool = Field(True)

    model_config = {
        "populate_by_name": True,
        "json_schema_extra": {
            "example": {
                "name": "hn front page",
                "kind": "scrape",
                "targetUrl": "https://news.ycombinator.com",
                "schedule": "@hourly",
            }
        },
    }


@router.post("/jobs", status_code=status.HTTP_201_CREATED)
async def create_job(req: JobCreate):
    _require_db()
    row = await repo.create_job(
        name=req.name, kind=req.kind, target_url=req.targetUrl,
        params=req.params, schedule=req.schedule, enabled=req.enabled,
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable — job not created.",
        )
    return job_to_api(row)


@router.get("/jobs")
async def list_jobs():
    _require_db()
    rows = await repo.list_jobs()
    return {"jobs": [job_to_api(r) for r in rows]}


@router.get("/jobs/{job_id}")
async def get_job(job_id: int):
    _require_db()
    row = await repo.get_job(job_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Job {job_id} not found.")
    return job_to_api(row)


@router.post("/jobs/{job_id}/run", status_code=status.HTTP_202_ACCEPTED)
async def run_job(job_id: int):
    _require_db()
    row = await repo.get_job(job_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Job {job_id} not found.")
    run_id = await runner.launch_job(row, trigger="manual")
    if run_id is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable — run not started.",
        )
    return {"success": True, "runId": run_id}
