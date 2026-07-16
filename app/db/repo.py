"""Async data-access layer for the persistence foundation.

Hard rules (preserve these):
  * Every function returns None / [] and writes nothing when persistence is
    disabled (pool is None). The disabled path is byte-for-byte the legacy
    behaviour.
  * Every database call is wrapped in try/except that logs and continues. A DB
    error must NEVER change an HTTP response or abort a scrape/crawl — files on
    disk remain the source of truth; the database is an additive index.

JSONB is passed as a json.dumps() string with an explicit ::jsonb cast (asyncpg
has no implicit dict->jsonb codec) and decoded with _loads() on read.
"""
import datetime
import json
import logging
from typing import Any, Dict, List, Optional

from app.db.pool import get_pool
from app import schedule_spec, webhooks

logger = logging.getLogger("db.repo")


def _loads(value: Any) -> Any:
    """asyncpg returns jsonb as str by default; decode to a Python value."""
    if isinstance(value, (str, bytes, bytearray)):
        try:
            return json.loads(value)
        except Exception:
            return None
    return value


def _row(record: Any) -> Optional[Dict[str, Any]]:
    return dict(record) if record is not None else None


# --- runs --------------------------------------------------------------------

async def record_run_start(
    *,
    external_id: Optional[str] = None,
    job_id: Optional[int] = None,
    trigger: str = "manual",
    status: str = "pending",
) -> Optional[int]:
    """Insert a scrape_runs row; returns its id (None when disabled/on error).

    started_at is stamped immediately when the run is created already-running
    (status='processing'); a 'pending' run gets started_at on mark_run_processing.
    """
    pool = await get_pool()
    if pool is None:
        return None
    try:
        async with pool.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO scrape_runs (external_id, job_id, trigger, status, started_at)
                VALUES ($1, $2, $3, $4,
                        CASE WHEN $4 IN ('processing','completed','failed') THEN now() END)
                RETURNING id
                """,
                external_id, job_id, trigger, status,
            )
    except Exception as e:
        logger.warning("record_run_start failed: %s", e)
        return None


async def mark_run_processing(
    run_id: Optional[int], *, external_id: Optional[str] = None
) -> None:
    pool = await get_pool()
    if pool is None or run_id is None:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE scrape_runs
                SET status = 'processing',
                    started_at = COALESCE(started_at, now()),
                    external_id = COALESCE($2, external_id)
                WHERE id = $1
                """,
                run_id, external_id,
            )
    except Exception as e:
        logger.warning("mark_run_processing failed: %s", e)


async def record_run_finish(
    run_id: Optional[int],
    *,
    status: str,
    engine_used: Optional[str] = None,
    pages_count: Optional[int] = None,
    error_message: Optional[str] = None,
    raw_output_path: Optional[str] = None,
    external_id: Optional[str] = None,
) -> None:
    pool = await get_pool()
    if pool is None or run_id is None:
        return
    ok = False
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE scrape_runs
                SET status = $2,
                    engine_used = COALESCE($3, engine_used),
                    pages_count = COALESCE($4, pages_count),
                    error_message = COALESCE($5, error_message),
                    raw_output_path = COALESCE($6, raw_output_path),
                    external_id = COALESCE($7, external_id),
                    finished_at = now()
                WHERE id = $1
                """,
                run_id, status, engine_used, pages_count,
                error_message, raw_output_path, external_id,
            )
        ok = True
    except Exception as e:
        logger.warning("record_run_finish failed: %s", e)
    # Single terminal-state dispatch point for run-completion webhooks (scrape and
    # crawl both finalize here). Opt-in + swallowed; never fails the run.
    if ok and status in ("completed", "failed"):
        try:
            await webhooks.deliver(await get_run(run_id))
        except Exception as e:
            logger.warning("webhook dispatch failed (run %s): %s", run_id, e)


async def record_page(
    run_id: Optional[int],
    *,
    url: Optional[str] = None,
    status_code: Optional[int] = None,
    engine: Optional[str] = None,
    extractor: Optional[str] = None,
    content_hash: Optional[str] = None,
    extracted_text: Optional[str] = None,
    raw_json_path: Optional[str] = None,
    raw_md_path: Optional[str] = None,
    raw_html_path: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[int]:
    pool = await get_pool()
    if pool is None:
        return None
    try:
        async with pool.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO scraped_pages
                    (run_id, url, status_code, engine, extractor, content_hash,
                     extracted_text, raw_json_path, raw_md_path, raw_html_path, metadata)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10, $11::jsonb)
                RETURNING id
                """,
                run_id, url, status_code, engine, extractor, content_hash,
                extracted_text, raw_json_path, raw_md_path, raw_html_path,
                json.dumps(metadata or {}),
            )
    except Exception as e:
        logger.warning("record_page failed: %s", e)
        return None


async def record_error(
    run_id: Optional[int],
    *,
    page_url: Optional[str] = None,
    stage: Optional[str] = None,
    message: Optional[str] = None,
) -> None:
    pool = await get_pool()
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO scrape_errors (run_id, page_url, stage, message)"
                " VALUES ($1,$2,$3,$4)",
                run_id, page_url, stage, (message or "")[:8000],
            )
    except Exception as e:
        logger.warning("record_error failed: %s", e)


async def get_run(run_id: int) -> Optional[Dict[str, Any]]:
    pool = await get_pool()
    if pool is None:
        return None
    try:
        async with pool.acquire() as conn:
            return _row(await conn.fetchrow("SELECT * FROM scrape_runs WHERE id = $1", run_id))
    except Exception as e:
        logger.warning("get_run failed: %s", e)
        return None


async def list_runs(
    *,
    job_id: Optional[int] = None,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """List scrape_runs newest-first, optionally scoped by job / status."""
    pool = await get_pool()
    if pool is None:
        return []
    where: List[str] = []
    params: List[Any] = []
    if job_id is not None:
        params.append(job_id)
        where.append(f"job_id = ${len(params)}")
    if status is not None:
        params.append(status)
        where.append(f"status = ${len(params)}")
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)
    lim = f"${len(params)}"
    params.append(offset)
    off = f"${len(params)}"
    sql = f"SELECT * FROM scrape_runs{clause} ORDER BY id DESC LIMIT {lim} OFFSET {off}"
    try:
        async with pool.acquire() as conn:
            return [dict(r) for r in await conn.fetch(sql, *params)]
    except Exception as e:
        logger.warning("list_runs failed: %s", e)
        return []


async def list_run_pages(run_id: int, limit: int = 1000) -> List[Dict[str, Any]]:
    pool = await get_pool()
    if pool is None:
        return []
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, url, status_code, engine, extractor, content_hash,"
                " raw_json_path, raw_md_path, raw_html_path, metadata, created_at"
                " FROM scraped_pages WHERE run_id = $1 ORDER BY id LIMIT $2",
                run_id, limit,
            )
            out = []
            for r in rows:
                d = dict(r)
                d["metadata"] = _loads(d.get("metadata"))
                out.append(d)
            return out
    except Exception as e:
        logger.warning("list_run_pages failed: %s", e)
        return []


async def get_last_page_by_url(url: str) -> Optional[Dict[str, Any]]:
    """Newest scraped_pages row for a URL (change-tracking fallback history).

    Uses idx_pages_url; returns None when disabled, on error, or never seen.
    """
    pool = await get_pool()
    if pool is None or not url:
        return None
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT content_hash, created_at FROM scraped_pages"
                " WHERE url = $1 ORDER BY id DESC LIMIT 1",
                url,
            )
            return _row(row)
    except Exception as e:
        logger.warning("get_last_page_by_url failed: %s", e)
        return None


# --- research runs -----------------------------------------------------------

def _parse_ts(value: Optional[str]) -> Optional[datetime.datetime]:
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value)
    except Exception:
        return None


async def upsert_research_run(job: Dict[str, Any]) -> None:
    """Index a research job's current state (create + every terminal edge).

    Additive only: the table is never read for resume — checkpoints under
    data/research/checkpoints are the source of truth. Swallowed like all
    DB work.
    """
    pool = await get_pool()
    if pool is None or not job.get("job_id"):
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO research_runs
                    (job_id, query, status, rounds_run, pages_scraped,
                     llm_calls, artifact_stem, started_at, finished_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (job_id) DO UPDATE SET
                    status        = EXCLUDED.status,
                    rounds_run    = EXCLUDED.rounds_run,
                    pages_scraped = EXCLUDED.pages_scraped,
                    llm_calls     = EXCLUDED.llm_calls,
                    artifact_stem = EXCLUDED.artifact_stem,
                    started_at    = EXCLUDED.started_at,
                    finished_at   = EXCLUDED.finished_at
                """,
                job.get("job_id"), job.get("query"),
                job.get("status") or "unknown",
                job.get("rounds_run"), job.get("pages_scraped"),
                job.get("llm_calls"), job.get("artifact_stem"),
                _parse_ts(job.get("start_time")), _parse_ts(job.get("end_time")),
            )
    except Exception as e:
        logger.warning("upsert_research_run failed: %s", e)


# --- extracted records -------------------------------------------------------

async def record_extracted_record(
    page_id: Optional[int],
    *,
    source_url: Optional[str] = None,
    record_type: str = "extract",
    data_json: Optional[Any] = None,
    content_hash: Optional[str] = None,
    confidence: Optional[float] = None,
) -> Optional[int]:
    """Insert one extracted_records row; returns its id (None when disabled)."""
    pool = await get_pool()
    if pool is None:
        return None
    try:
        async with pool.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO extracted_records
                    (page_id, source_url, record_type, data_json, content_hash, confidence)
                VALUES ($1,$2,$3,$4::jsonb,$5,$6)
                RETURNING id
                """,
                page_id, source_url, record_type,
                json.dumps(data_json) if data_json is not None else None,
                content_hash, confidence,
            )
    except Exception as e:
        logger.warning("record_extracted_record failed: %s", e)
        return None


async def list_records(
    *,
    run_id: Optional[int] = None,
    job_id: Optional[int] = None,
    page_id: Optional[int] = None,
    record_type: Optional[str] = None,
    limit: int = 1000,
) -> List[Dict[str, Any]]:
    """List extracted_records, optionally scoped by run / job / page / type.

    Joins through scraped_pages (and scrape_runs for job_id). data_json is decoded.
    """
    pool = await get_pool()
    if pool is None:
        return []
    where: List[str] = []
    params: List[Any] = []
    join_runs = job_id is not None
    if run_id is not None:
        params.append(run_id)
        where.append(f"p.run_id = ${len(params)}")
    if job_id is not None:
        params.append(job_id)
        where.append(f"r.job_id = ${len(params)}")
    if page_id is not None:
        params.append(page_id)
        where.append(f"er.page_id = ${len(params)}")
    if record_type is not None:
        params.append(record_type)
        where.append(f"er.record_type = ${len(params)}")
    params.append(limit)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    sql = (
        "SELECT er.* FROM extracted_records er"
        " JOIN scraped_pages p ON er.page_id = p.id"
        + (" JOIN scrape_runs r ON p.run_id = r.id" if join_runs else "")
        + clause
        + f" ORDER BY er.id LIMIT ${len(params)}"
    )
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
            out = []
            for r in rows:
                d = dict(r)
                d["data_json"] = _loads(d.get("data_json"))
                out.append(d)
            return out
    except Exception as e:
        logger.warning("list_records failed: %s", e)
        return []


# --- search + export ---------------------------------------------------------

async def search_pages(
    q: str,
    *,
    lang: Optional[str] = None,
    license: Optional[str] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Full-text search over scraped_pages via the generated tsvector + GIN.

    websearch_to_tsquery gives users Google-ish syntax (quoted phrases, OR, -).
    Naming the config makes it IMMUTABLE (planned once). Results are ranked by
    ts_rank with a ts_headline snippet; optional language/license filters apply
    to the verbatim metadata. Returns [] on empty query or when disabled.
    """
    pool = await get_pool()
    if pool is None or not (q or "").strip():
        return []
    params: List[Any] = [q]
    filters = ""
    if lang:
        params.append(lang)
        filters += f" AND metadata->>'language' = ${len(params)}"
    if license:
        params.append(license)
        filters += f" AND metadata->'license'->>'id' = ${len(params)}"
    params.append(limit)
    sql = (
        "SELECT id, url, run_id, content_hash, raw_json_path, metadata,"
        " ts_rank(search_tsv, query) AS rank,"
        " ts_headline('english', coalesce(extracted_text, ''), query,"
        "   'MaxFragments=2,MinWords=5,MaxWords=18,StartSel=<b>,StopSel=</b>') AS snippet"
        " FROM scraped_pages, websearch_to_tsquery('english', $1) query"
        " WHERE search_tsv @@ query" + filters +
        f" ORDER BY rank DESC, id LIMIT ${len(params)}"
    )
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
            out = []
            for r in rows:
                d = dict(r)
                d["metadata"] = _loads(d.get("metadata"))
                out.append(d)
            return out
    except Exception as e:
        logger.warning("search_pages failed: %s", e)
        return []


def _export_scope(run_id: Optional[int], job_id: Optional[int]):
    """(where-clause, params, needs_runs_join) for run/job scoped exports."""
    if run_id is not None:
        return "p.run_id = $1", [run_id], False
    if job_id is not None:
        return "r.job_id = $1", [job_id], True
    return None, [], False


async def iter_pages_for_export(*, run_id=None, job_id=None):
    """Yield scraped_pages rows for a run or job, streamed via a server-side
    cursor (never buffering the whole result set). Decodes metadata per row."""
    pool = await get_pool()
    where, params, join_runs = _export_scope(run_id, job_id)
    if pool is None or where is None:
        return
    sql = (
        "SELECT p.id, p.url, p.status_code, p.engine, p.extractor, p.content_hash,"
        " p.raw_json_path, p.raw_md_path, p.raw_html_path, p.metadata, p.created_at"
        " FROM scraped_pages p"
        + (" JOIN scrape_runs r ON p.run_id = r.id" if join_runs else "")
        + f" WHERE {where} ORDER BY p.id"
    )
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                async for r in conn.cursor(sql, *params):
                    d = dict(r)
                    d["metadata"] = _loads(d.get("metadata"))
                    yield d
    except Exception as e:
        logger.warning("iter_pages_for_export failed: %s", e)


async def iter_records_for_export(*, run_id=None, job_id=None, record_type=None):
    """Yield extracted_records rows for a run or job, streamed via a cursor."""
    pool = await get_pool()
    where, params, join_runs = _export_scope(run_id, job_id)
    if pool is None or where is None:
        return
    if record_type is not None:
        params.append(record_type)
        where += f" AND er.record_type = ${len(params)}"
    sql = (
        "SELECT er.id, er.page_id, er.source_url, er.record_type, er.data_json,"
        " er.content_hash, er.confidence, er.created_at"
        " FROM extracted_records er JOIN scraped_pages p ON er.page_id = p.id"
        + (" JOIN scrape_runs r ON p.run_id = r.id" if join_runs else "")
        + f" WHERE {where} ORDER BY er.id"
    )
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                async for r in conn.cursor(sql, *params):
                    d = dict(r)
                    d["data_json"] = _loads(d.get("data_json"))
                    yield d
    except Exception as e:
        logger.warning("iter_records_for_export failed: %s", e)


# --- job definitions ---------------------------------------------------------

async def create_job(
    *,
    name: Optional[str],
    kind: str = "scrape",
    target_url: Optional[str],
    params: Optional[Dict[str, Any]] = None,
    schedule: Optional[str] = None,
    enabled: bool = True,
    next_run_at: Optional[datetime.datetime] = None,
) -> Optional[Dict[str, Any]]:
    pool = await get_pool()
    if pool is None:
        return None
    # First fire defaults to one interval out unless caller fixed it explicitly.
    if next_run_at is None and enabled:
        next_run_at = schedule_spec.next_run_at(schedule)
    try:
        async with pool.acquire() as conn:
            return _row(await conn.fetchrow(
                """
                INSERT INTO scrape_jobs (name, kind, target_url, params, schedule, enabled, next_run_at)
                VALUES ($1,$2,$3,$4::jsonb,$5,$6,$7)
                RETURNING *
                """,
                name, kind, target_url, json.dumps(params or {}),
                schedule, enabled, next_run_at,
            ))
    except Exception as e:
        logger.warning("create_job failed: %s", e)
        return None


async def get_job(job_id: int) -> Optional[Dict[str, Any]]:
    pool = await get_pool()
    if pool is None:
        return None
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM scrape_jobs WHERE id = $1", job_id)
            d = _row(row)
            if d is not None:
                d["params"] = _loads(d.get("params"))
            return d
    except Exception as e:
        logger.warning("get_job failed: %s", e)
        return None


async def list_jobs(limit: int = 500) -> List[Dict[str, Any]]:
    pool = await get_pool()
    if pool is None:
        return []
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM scrape_jobs ORDER BY id DESC LIMIT $1", limit
            )
            out = []
            for r in rows:
                d = dict(r)
                d["params"] = _loads(d.get("params"))
                out.append(d)
            return out
    except Exception as e:
        logger.warning("list_jobs failed: %s", e)
        return []


async def claim_due_jobs(limit: int = 10) -> List[Dict[str, Any]]:
    """Atomically claim due job definitions and advance their next_run_at.

    Uses FOR UPDATE SKIP LOCKED so this is correct even if the deploy ever moves
    to multiple workers (today it is single-process). For each due job we reschedule
    inside the lock, then return only those with no in-flight run (the overlap
    guard) for the caller to launch *after* the transaction commits.
    """
    pool = await get_pool()
    if pool is None:
        return []
    launch: List[Dict[str, Any]] = []
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    """
                    SELECT * FROM scrape_jobs
                    WHERE enabled AND next_run_at IS NOT NULL AND next_run_at <= now()
                    ORDER BY next_run_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT $1
                    """,
                    limit,
                )
                for r in rows:
                    job = dict(r)
                    job["params"] = _loads(job.get("params"))
                    active = await conn.fetchval(
                        "SELECT 1 FROM scrape_runs"
                        " WHERE job_id = $1 AND status IN ('pending','processing') LIMIT 1",
                        job["id"],
                    )
                    nxt = schedule_spec.next_run_at(job.get("schedule"))
                    await conn.execute(
                        "UPDATE scrape_jobs"
                        " SET last_run_at = now(), next_run_at = $2, updated_at = now()"
                        " WHERE id = $1",
                        job["id"], nxt,
                    )
                    if not active:
                        launch.append(job)
    except Exception as e:
        logger.warning("claim_due_jobs failed: %s", e)
    return launch
