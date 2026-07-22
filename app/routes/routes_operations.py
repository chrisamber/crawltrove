"""Health, metrics, and authenticated durable-operations reads."""
from __future__ import annotations

import base64
import json
import os
import resource
import secrets
import sys
import time
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app import admin
from app import metrics as operational_metrics
from app.artifacts import artifact_store
from app.crawl import repository
from app.crawl.service import crawl_service
from app.db import migrate
from app.db.pool import get_pool


router = APIRouter(prefix="/api/operations", tags=["operations"])
health_router = APIRouter(tags=["operations"])
metrics_router = APIRouter(tags=["operations"])


def _enabled(value: str | None) -> bool:
    return (value or "").lower() in {"1", "true", "yes", "on"}


def _auth_configured() -> bool:
    return bool(os.getenv("APP_PASSWORD")) or bool(os.getenv("API_KEYS", "").strip())


def metrics_auth_bypass_allowed() -> bool:
    """Allow unauthenticated scraping only on an explicitly loopback listener."""
    published = os.getenv("PUBLISHED_BIND_ADDRESS", "").strip().lower()
    return _enabled(os.getenv("METRICS_BIND_INTERNAL")) and published in {
        "127.0.0.1", "::1", "localhost",
    }


def _operations_authorized(request: Request) -> bool:
    keys = [key.strip() for key in os.getenv("API_KEYS", "").split(",") if key.strip()]
    candidate = request.headers.get("x-api-key", "")
    if candidate and any(secrets.compare_digest(candidate, key) for key in keys):
        return True
    password = os.getenv("APP_PASSWORD")
    if not password:
        return False
    header = request.headers.get("authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        username, _, supplied = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except Exception:
        return False
    return secrets.compare_digest(username, os.getenv("APP_USERNAME", "admin")) and secrets.compare_digest(
        supplied, password,
    )


async def require_operations_auth(request: Request) -> None:
    """Match the app's optional API/Basic authentication for operations reads."""
    if not _auth_configured():
        return
    if not _operations_authorized(request):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="authentication required")


async def _database_ready() -> bool:
    pool = await get_pool()
    if pool is None:
        return False
    try:
        async with pool.acquire() as conn:
            return bool(await conn.fetchval("SELECT 1"))
    except Exception:
        return False


async def _migrations_ready() -> bool:
    pool = await get_pool()
    if pool is None:
        return False
    try:
        async with pool.acquire() as conn:
            applied = {row["version"] for row in await conn.fetch("SELECT version FROM schema_migrations")}
        expected = {version for version, _path in migrate._migration_files()}
    except Exception:
        return False
    return applied == expected


async def _artifacts_ready() -> bool:
    try:
        return bool(await artifact_store().healthcheck())
    except Exception:
        return False


async def _browser_ready() -> bool:
    if not _enabled(os.getenv("CRAWLTROVE_REQUIRE_BROWSER")):
        return True
    if _enabled(os.getenv("CRAWLTROVE_REMOTE_WORKERS")):
        pool = await get_pool()
        if pool is None:
            return False
        try:
            async with pool.acquire() as conn:
                return bool(await conn.fetchval(
                    """SELECT EXISTS (
                           SELECT 1 FROM workers
                           WHERE state = 'active' AND 'browser' = ANY(capabilities)
                             AND last_seen_at > now() - interval '90 seconds'
                       )"""
                ))
        except Exception:
            return False
    browser = getattr(getattr(crawl_service._worker, "scraper", None), "browser", None)
    if browser is None:
        return False
    try:
        await browser.start()
    except Exception:
        return False
    return True


async def _leases_ready() -> bool:
    task = crawl_service._maintenance_task
    last_success = crawl_service._maintenance_last_success
    return (
        task is not None and not task.done() and last_success is not None
        and time.monotonic() - last_success <= 30
    )


async def readiness_report(checks: dict[str, bool] | None = None) -> tuple[dict[str, str], bool]:
    """Return compatibility health plus a strict readiness result."""
    if checks is None:
        checks = {
            "database": await _database_ready(),
            "artifacts": await _artifacts_ready(),
            "migrations": await _migrations_ready(),
            "browser": await _browser_ready(),
            "leases": await _leases_ready(),
        }
    ready = all(checks.get(name, False) for name in (
        "database", "artifacts", "migrations", "browser", "leases",
    ))
    return {
        "status": "ready" if ready else "not_ready",
        "database": "up" if checks.get("database") else "down",
        "artifacts": "up" if checks.get("artifacts") else "down",
        "migrations": "compatible" if checks.get("migrations") else "incompatible",
        "providers": "available",
    }, ready


@health_router.get("/health/live")
async def health_live():
    return {"status": "alive"}


@health_router.get("/health/ready")
async def health_ready():
    report, ready = await readiness_report()
    if not ready:
        return Response(
            content=json.dumps(report), media_type="application/json",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return report


@metrics_router.get("/metrics")
async def metrics(request: Request):
    if not metrics_auth_bypass_allowed():
        await require_operations_auth(request)
    await _refresh_metrics()
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


async def _refresh_metrics() -> None:
    """Project durable state into bounded metric series before each scrape."""
    pool = await get_pool()
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            task_rows = await conn.fetch(
                "SELECT state, count(*) AS count FROM crawl_tasks GROUP BY state"
            )
            origin_rows = await conn.fetch(
                "SELECT circuit_state AS state, count(*) AS count "
                "FROM crawl_origins GROUP BY circuit_state"
            )
            worker_rows = await conn.fetch(
                """SELECT state, capability, count(*) AS count
                   FROM workers CROSS JOIN LATERAL unnest(capabilities) AS capability
                   GROUP BY state, capability"""
            )
            session_rows = await conn.fetch(
                """SELECT state, backend, count(*) AS count FROM live_sessions
                   GROUP BY state, backend"""
            )
            attempt_rows = await conn.fetch(
                """SELECT route, COALESCE(provider, 'unknown') AS provider,
                          COALESCE(outcome, 'unknown') AS outcome, count(*) AS count
                   FROM acquisition_attempts
                   WHERE route = ANY($1::TEXT[]) AND finished_at IS NOT NULL
                   GROUP BY route, provider, outcome""",
                sorted(operational_metrics.LABEL_VALUES["route"] - {"unknown"}),
            )
            usage_rows = await conn.fetch(
                """SELECT provider, meter, sum(consumed_value) AS amount
                   FROM crawl_provider_usage GROUP BY provider, meter"""
            )
    except Exception:
        return
    operational_metrics.refresh_durable_metrics({
        "tasks": [(row["state"], row["count"]) for row in task_rows],
        "origins": [(row["state"], row["count"]) for row in origin_rows],
        "workers": [
            (row["state"], row["capability"], row["count"]) for row in worker_rows
        ],
        "sessions": [
            (row["state"], row["backend"], row["count"]) for row in session_rows
        ],
        "attempts": [
            (row["route"], row["provider"], row["outcome"], row["count"])
            for row in attempt_rows
        ],
        "usage": [
            (row["provider"], row["meter"], row["amount"]) for row in usage_rows
        ],
    })
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    operational_metrics.memory.labels("process").set(
        rss if sys.platform == "darwin" else rss * 1024,
    )


async def _operations_pool():
    try:
        return await repository.require_pool()
    except repository.PersistenceUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.get("/workers", dependencies=[Depends(require_operations_auth)])
async def workers_read():
    pool = await _operations_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, capabilities, protocol_version, state, last_seen_at, created_at FROM workers ORDER BY id"
        )
    return {"workers": [dict(row) for row in rows]}


@router.get("/providers", dependencies=[Depends(require_operations_auth)])
async def providers_read():
    return {"providers": crawl_service.registry.health()}


@router.get("/sessions", dependencies=[Depends(require_operations_auth)])
async def sessions_read():
    pool = await _operations_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, task_id, backend, worker_id, state, expires_at, last_seen_at, created_at, closed_at "
            "FROM live_sessions ORDER BY created_at DESC LIMIT 200"
        )
    return {"sessions": [dict(row) for row in rows]}


@router.get("/attempts", dependencies=[Depends(require_operations_auth)])
async def attempts_read(job_id: Annotated[UUID, Query(alias="jobId")]):
    pool = await _operations_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, task_id, attempt_number, route, provider, worker_id, started_at, finished_at, "
            "duration_ms, reserved_cost, actual_cost, outcome, block_reason, error_code "
            "FROM acquisition_attempts WHERE job_id = $1 ORDER BY started_at DESC LIMIT 500",
            job_id,
        )
    return {"attempts": [dict(row) for row in rows]}


@router.get("/failures", dependencies=[Depends(require_operations_auth)])
async def failures_read(
    job_id: Annotated[UUID | None, Query(alias="jobId")] = None,
):
    try:
        return {"failures": await admin.list_failures(job_id)}
    except repository.PersistenceUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
