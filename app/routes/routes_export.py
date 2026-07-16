"""REST: export, records listing, and full-text search over the persisted index.

    GET /api/export.csv?runId=|jobId=     stream scraped_pages as CSV
    GET /api/export.json?runId=|jobId=    stream scraped_pages as a JSON array
    GET /api/records?runId=|jobId=&recordType=   list extracted_records (camelCase)
    GET /api/search?q=&lang=&license=     Postgres FTS over scraped_pages

Exports stream straight from a server-side cursor (repo.iter_*_for_export) so a
large run never buffers in memory. All endpoints require persistence; they return
503 when DATABASE_URL is unset, and exports require a runId or jobId selector.
"""
import csv
import io
import json
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from app.db import pool, repo
from app.routes.serialize import _iso, page_to_api, record_to_api, search_hit_to_api

router = APIRouter(prefix="/api", tags=["export"])


def _require_db() -> None:
    if not pool.enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Persistence is not configured — set DATABASE_URL to use export/search.",
        )


def _require_scope(run_id: Optional[int], job_id: Optional[int]) -> None:
    if run_id is None and job_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide a runId or jobId to scope the export.",
        )


_CSV_COLUMNS = [
    "id", "url", "status_code", "engine", "extractor", "content_hash",
    "language", "license", "raw_json_path", "raw_md_path", "raw_html_path",
    "created_at",
]


def _csv_value(d: dict) -> list:
    meta = d.get("metadata") or {}
    lic = meta.get("license")
    return [
        d.get("id"), d.get("url"), d.get("status_code"), d.get("engine"),
        d.get("extractor"), d.get("content_hash"), meta.get("language"),
        lic.get("id") if isinstance(lic, dict) else lic,
        d.get("raw_json_path"), d.get("raw_md_path"), d.get("raw_html_path"),
        _iso(d.get("created_at")),
    ]


@router.get("/export.csv")
async def export_csv(runId: Optional[int] = None, jobId: Optional[int] = None):
    _require_db()
    _require_scope(runId, jobId)

    async def gen():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(_CSV_COLUMNS)
        yield buf.getvalue()
        buf.seek(0); buf.truncate(0)
        async for d in repo.iter_pages_for_export(run_id=runId, job_id=jobId):
            writer.writerow(_csv_value(d))
            yield buf.getvalue()
            buf.seek(0); buf.truncate(0)

    return StreamingResponse(
        gen(), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=pages.csv"})


@router.get("/export.json")
async def export_json(runId: Optional[int] = None, jobId: Optional[int] = None):
    _require_db()
    _require_scope(runId, jobId)

    async def gen():
        yield "["
        first = True
        async for d in repo.iter_pages_for_export(run_id=runId, job_id=jobId):
            chunk = json.dumps(page_to_api(d), default=str)
            yield chunk if first else "," + chunk
            first = False
        yield "]"

    return StreamingResponse(gen(), media_type="application/json")


@router.get("/records")
async def list_records(
    runId: Optional[int] = None,
    jobId: Optional[int] = None,
    pageId: Optional[int] = None,
    recordType: Optional[str] = None,
    limit: int = Query(1000, ge=1, le=10000),
):
    _require_db()
    rows = await repo.list_records(
        run_id=runId, job_id=jobId, page_id=pageId,
        record_type=recordType, limit=limit)
    return {"records": [record_to_api(r) for r in rows]}


@router.get("/search")
async def search(
    q: str = Query(..., min_length=1),
    lang: Optional[str] = None,
    license: Optional[str] = None,
    limit: int = Query(20, ge=1, le=200),
):
    _require_db()
    rows = await repo.search_pages(q, lang=lang, license=license, limit=limit)
    return {"query": q, "results": [search_hit_to_api(r) for r in rows]}
