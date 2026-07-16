"""REST: list executions and read a single one (scrape_runs) with its pages.

    GET /api/runs            -> recent runs (filter by jobId/status, paginated)
    GET /api/runs/{id}       -> the run plus a summary of its scraped_pages

Requires persistence (DATABASE_URL); returns 503 when disabled, 404 when the run
id is unknown.
"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status

from app.db import pool, repo
from app.routes.serialize import run_to_api, page_to_api

router = APIRouter(prefix="/api", tags=["runs"])


def _require_db() -> None:
    if not pool.enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Persistence is not configured — set DATABASE_URL to use runs.",
        )


@router.get("/runs")
async def list_runs(
    jobId: Optional[int] = None,
    status: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    _require_db()
    rows = await repo.list_runs(job_id=jobId, status=status, limit=limit, offset=offset)
    return {"runs": [run_to_api(r) for r in rows]}


@router.get("/runs/{run_id}")
async def get_run(run_id: int):
    _require_db()
    row = await repo.get_run(run_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Run {run_id} not found.")
    pages = await repo.list_run_pages(run_id)
    out = run_to_api(row)
    out["pages"] = [page_to_api(p) for p in pages]
    return out
