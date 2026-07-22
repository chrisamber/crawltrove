"""Durable crawl persistence.

Unlike the legacy persistence index, durable crawl operations require a live
database: accepting a crawl without one would create work that cannot recover.
"""
import hashlib
import json
import math
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional
from uuid import UUID

from app import normalize
from app.acquisition.providers import ProviderProtocolError, ROUTE_NATIVE_METERS
from app.crawl.config import CrawlConfig
from app.crawl.types import ClaimedTask, TaskResult
from app.db.pool import get_pool

if TYPE_CHECKING:
    from app.crawl.classify import FailureDecision


LEASE_SECONDS = 120
HTML_RESPONSE_CAP = 10 * 1024 * 1024
DOCUMENT_RESPONSE_CAP = 50 * 1024 * 1024
IMAGE_RESPONSE_CAP = 25 * 1024 * 1024
INLINE_ARTIFACT_CAP = 30 * 1024 * 1024
DOCUMENT_SUFFIXES = (".pdf", ".epub")
IMAGE_SUFFIXES = (".avif", ".gif", ".jpeg", ".jpg", ".png", ".webp")


class PersistenceUnavailable(RuntimeError):
    """Raised when durable crawl storage is not configured or reachable."""


@dataclass(frozen=True)
class ReservedAcquisitionAttempt:
    """One provider call whose native cost has been reserved."""

    id: UUID
    task_id: UUID
    route: str
    provider: str
    reserved_cost: dict[str, int | float]


async def require_pool() -> Any:
    """Return the durable-crawl pool or fail before accepting work."""
    pool = await get_pool()
    if pool is None:
        raise PersistenceUnavailable("Durable crawling requires PostgreSQL")
    return pool


def _loads(value: Any) -> Any:
    if isinstance(value, (str, bytes, bytearray)):
        return json.loads(value)
    return value


def _provider_usage_rows(config: CrawlConfig) -> tuple[tuple[str, str, int | float], ...]:
    budgets = config.acquisition.creditBudgets
    rows: list[tuple[str, str, int | float]] = []
    if budgets.firecrawl is not None:
        rows.append(("firecrawl", "credits", budgets.firecrawl.credits))
    if budgets.brightdata is not None:
        rows.append(("brightdata", "requests", budgets.brightdata.requests))
    if budgets.browserbase is not None:
        rows.extend((
            ("browserbase", "browserMinutes", budgets.browserbase.browserMinutes),
            ("browserbase", "proxyBytes", budgets.browserbase.proxyBytes),
        ))
    return tuple(rows)


def _native_cost(route: str, values: Any) -> tuple[str, dict[str, int | float]]:
    """Reject ambiguous or impossible native meter values before a DB write."""
    try:
        provider, expected = ROUTE_NATIVE_METERS[route]
    except KeyError as exc:
        raise ProviderProtocolError(f"unknown acquisition route: {route}") from exc
    if not isinstance(values, Mapping) or set(values) != expected:
        raise ProviderProtocolError(
            f"{route} must report exactly {sorted(expected)!r} native meter keys"
        )
    normalized: dict[str, int | float] = {}
    for meter, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ProviderProtocolError(f"{route} meter {meter} is not numeric")
        if not math.isfinite(value) or value < 0:
            raise ProviderProtocolError(f"{route} meter {meter} is invalid")
        normalized[meter] = value
    if route == "browserbase_session" and normalized["proxyBytes"] != 0:
        raise ProviderProtocolError("Browserbase managed proxies are disabled")
    return provider, normalized


async def _event(
    conn: Any, job_id: UUID, task_id: Optional[UUID], event: str, metadata: dict
) -> None:
    await conn.execute(
        """INSERT INTO crawl_events (job_id, task_id, event, metadata)
           VALUES ($1, $2, $3, $4::jsonb)""",
        job_id, task_id, event, json.dumps(metadata),
    )


async def submit_job(
    config: CrawlConfig,
    idempotency_key: Optional[str] = None,
    definition_job_id: Optional[int] = None,
    trigger: str = "manual",
) -> UUID:
    """Create one job and its seed task, returning an existing keyed job if any."""
    pool = await require_pool()
    normalized = normalize.normalize_url(config.url)
    origin_key = normalize.origin_key(normalized)
    url_hash = hashlib.sha256(normalized.encode("utf-8")).digest()
    job_id, task_id = uuid.uuid4(), uuid.uuid4()

    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                if idempotency_key:
                    existing = await conn.fetchval(
                        "SELECT id FROM crawl_jobs WHERE idempotency_key = $1",
                        idempotency_key,
                    )
                    if existing:
                        return existing
                run_id = await conn.fetchval(
                    """INSERT INTO scrape_runs (external_id, job_id, trigger, status)
                       VALUES ($1, $2, $3, 'pending') RETURNING id""",
                    str(job_id), definition_job_id, trigger,
                )
                await conn.execute(
                    """INSERT INTO crawl_jobs
                       (id, run_id, state, config, max_pages, max_bytes,
                        max_artifact_bytes, discovered_count, deadline_at,
                        idempotency_key)
                       VALUES ($1, $2, 'pending', $3::jsonb, $4, $5, $6, 1,
                               now() + make_interval(secs => $7), $8)""",
                    job_id, run_id, config.model_dump_json(), config.limit,
                    config.maxBytes, config.maxArtifactBytes, config.timeoutSeconds,
                    idempotency_key,
                )
                for provider, meter, limit_value in _provider_usage_rows(config):
                    await conn.execute(
                        """INSERT INTO crawl_provider_usage
                           (job_id, provider, meter, limit_value)
                           VALUES ($1, $2, $3, $4)""",
                        job_id, provider, meter, limit_value,
                    )
                await conn.execute(
                    """INSERT INTO crawl_tasks
                       (id, job_id, original_url, normalized_url, url_hash, origin_key,
                        depth, discovery_seq, state, max_attempts, required_capabilities)
                       VALUES ($1, $2, $3, $4, $5, $6, 0, 0, 'pending', $7,
                               $8::TEXT[])""",
                    task_id, job_id, config.url, normalized, url_hash, origin_key,
                    config.acquisition.maxAttempts,
                    ["browser"] if config.engine == "browser" else ["http"],
                )
                await conn.execute(
                    "INSERT INTO crawl_origins (origin_key) VALUES ($1) "
                    "ON CONFLICT DO NOTHING",
                    origin_key,
                )
                await _event(conn, job_id, task_id, "job_submitted", {})
        except Exception as exc:
            if not idempotency_key or getattr(exc, "constraint_name", None) != (
                "crawl_jobs_idempotency_key_key"
            ):
                raise
            # The transaction rolled back. A uniqueness conflict waits for the
            # winner to commit, so its row is visible in this fresh statement.
            existing = await conn.fetchval(
                "SELECT id FROM crawl_jobs WHERE idempotency_key = $1",
                idempotency_key,
            )
            if existing is None:
                raise
            return existing
    return job_id


async def get_job(job_id: UUID) -> Optional[dict]:
    """Return a durable job with results and failures in discovery order."""
    pool = await require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM crawl_jobs WHERE id = $1", job_id)
        if row is None:
            return None
        job = dict(row)
        job["config"] = _loads(job["config"])
        result_rows = await conn.fetch(
            """SELECT r.*, t.discovery_seq
               FROM crawl_results r JOIN crawl_tasks t ON t.id = r.task_id
               WHERE t.job_id = $1
               ORDER BY t.discovery_seq""",
            job_id,
        )
        job["results"] = []
        for row in result_rows:
            result = dict(row)
            result["metadata"] = _loads(result["metadata"])
            job["results"].append(result)
        error_rows = await conn.fetch(
            """SELECT original_url AS url, state, http_status, error_class,
                      error_code, error_message, retry_after_at, finished_at
               FROM crawl_tasks
               WHERE job_id = $1 AND error_code IS NOT NULL
               ORDER BY discovery_seq""",
            job_id,
        )
        job["errors"] = [dict(row) for row in error_rows]
        return job


async def request_cancel(job_id: UUID) -> bool:
    """Stop unleased work and finish cancellation immediately when safe."""
    pool = await require_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            job = await conn.fetchrow(
                "SELECT state FROM crawl_jobs WHERE id = $1 FOR UPDATE", job_id
            )
            if job is None:
                return False
            if job["state"] in {"completed", "partial", "failed", "cancelled", "timed_out"}:
                return True
            cancelled = await conn.fetch(
                """UPDATE crawl_tasks
                   SET state = 'cancelled', finished_at = now(), updated_at = now()
                   WHERE job_id = $1
                     AND state IN ('pending', 'retry_wait', 'waiting_input')
                   RETURNING id""",
                job_id,
            )
            active = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM crawl_tasks "
                "WHERE job_id = $1 AND state = 'leased')",
                job_id,
            )
            row = await conn.fetchrow(
                """UPDATE crawl_jobs
                   SET cancel_requested_at = COALESCE(cancel_requested_at, now()),
                       terminal_count = terminal_count + $2,
                       state = CASE WHEN $3 THEN state ELSE 'cancelled' END,
                       finished_at = CASE WHEN $3 THEN finished_at ELSE now() END
                   WHERE id = $1
                   RETURNING state, run_id""",
                job_id, len(cancelled), active,
            )
            if row is not None and row["state"] == "cancelled":
                await conn.execute(
                    "UPDATE scrape_runs SET status = 'cancelled', finished_at = now() "
                    "WHERE id = $1",
                    row["run_id"],
                )
            await _event(conn, job_id, None, "job_cancel_requested", {})
            return row is not None


async def list_pages(
    job_id: UUID, after: Optional[int] = None, limit: int = 50
) -> Optional[list[dict]]:
    """Return cursor-paginated crawl pages, including accessible result content."""
    pool = await require_pool()
    async with pool.acquire() as conn:
        if not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM crawl_jobs WHERE id = $1)", job_id,
        ):
            return None
        rows = await conn.fetch(
            """SELECT t.discovery_seq, t.original_url, t.normalized_url, t.state,
                      r.final_url, r.status_code, r.title, r.markdown, r.markdown_ref,
                      r.metadata,
                      r.downloaded_bytes, r.artifact_bytes, r.created_at
               FROM crawl_tasks t LEFT JOIN crawl_results r ON r.task_id = t.id
               WHERE t.job_id = $1 AND t.discovery_seq > $2
               ORDER BY t.discovery_seq LIMIT $3""",
            job_id, -1 if after is None else after, max(1, min(limit, 100)),
        )
    pages = []
    for row in rows:
        page = dict(row)
        if page["metadata"] is not None:
            page["metadata"] = _loads(page["metadata"])
        pages.append(page)
    return pages


_TERMINAL_TASK_STATES = (
    "succeeded", "http_error", "blocked_robots", "extraction_failed",
    "permanent_failed", "cancelled",
)


async def reconcile_job(job_id: UUID) -> bool:
    """Recompute cached job counters from task and result rows."""
    pool = await require_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            job = await conn.fetchrow(
                "SELECT id FROM crawl_jobs WHERE id = $1 FOR UPDATE", job_id
            )
            if job is None:
                return False
            counters = await conn.fetchrow(
                """SELECT count(*) AS discovered_count,
                          count(*) FILTER (WHERE state = ANY($2::TEXT[])) AS terminal_count,
                          count(*) FILTER (WHERE state = 'succeeded') AS succeeded_count,
                          count(*) FILTER (WHERE state IN
                              ('http_error', 'extraction_failed', 'permanent_failed')) AS failed_count,
                          count(*) FILTER (WHERE state = 'blocked_robots') AS blocked_count,
                          COALESCE(sum(byte_budget_reserved), 0) AS reserved_bytes,
                          COALESCE(sum(artifact_budget_reserved), 0) AS reserved_artifact_bytes,
                          COALESCE(max(discovery_seq) + 1, 1) AS next_discovery_seq
                   FROM crawl_tasks WHERE job_id = $1""",
                job_id, list(_TERMINAL_TASK_STATES),
            )
            usage = await conn.fetchrow(
                """SELECT COALESCE(sum((actual_cost ->> 'downloaded_bytes')::BIGINT), 0)
                              AS downloaded_bytes,
                          COALESCE(sum((actual_cost ->> 'artifact_bytes')::BIGINT), 0)
                              AS artifact_bytes
                   FROM acquisition_attempts WHERE job_id = $1""",
                job_id,
            )
            await conn.execute(
                """UPDATE crawl_jobs
                   SET discovered_count = $2, terminal_count = $3, succeeded_count = $4,
                       failed_count = $5, blocked_count = $6, reserved_bytes = $7,
                       reserved_artifact_bytes = $8, next_discovery_seq = $9,
                       downloaded_bytes = $10, artifact_bytes = $11
                   WHERE id = $1""",
                job_id, counters["discovered_count"], counters["terminal_count"],
                counters["succeeded_count"], counters["failed_count"],
                counters["blocked_count"], counters["reserved_bytes"],
                counters["reserved_artifact_bytes"], counters["next_discovery_seq"],
                usage["downloaded_bytes"], usage["artifact_bytes"],
            )
    return True


async def finalize_jobs() -> int:
    """Finish drained jobs and convert expired queued work into terminal tasks."""
    pool = await require_pool()
    finalized = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            jobs = await conn.fetch(
                """SELECT * FROM crawl_jobs
                   WHERE state IN ('pending', 'running')
                   FOR UPDATE SKIP LOCKED"""
            )
            for job in jobs:
                expired = await conn.fetchval("SELECT $1 <= now()", job["deadline_at"])
                if expired:
                    cancelled = await conn.fetch(
                        """UPDATE crawl_tasks
                           SET state = 'cancelled', finished_at = now(), updated_at = now()
                           WHERE job_id = $1
                             AND state IN ('pending', 'retry_wait', 'waiting_input')
                           RETURNING id""",
                        job["id"],
                    )
                    if cancelled:
                        await conn.execute(
                            "UPDATE crawl_jobs SET terminal_count = terminal_count + $2 "
                            "WHERE id = $1", job["id"], len(cancelled),
                        )
                nonterminal = await conn.fetchval(
                    """SELECT EXISTS (
                           SELECT 1 FROM crawl_tasks
                           WHERE job_id = $1 AND state <> ALL($2::TEXT[])
                       )""",
                    job["id"], list(_TERMINAL_TASK_STATES),
                )
                if nonterminal:
                    continue
                aggregate = await conn.fetchrow(
                    """SELECT count(*) FILTER (WHERE state = 'succeeded') AS succeeded,
                              count(*) FILTER (WHERE state = 'blocked_robots') AS blocked,
                              count(*) FILTER (WHERE state IN
                                  ('http_error', 'extraction_failed', 'permanent_failed')) AS failed
                       FROM crawl_tasks WHERE job_id = $1""",
                    job["id"],
                )
                if job["cancel_requested_at"] is not None:
                    state = "cancelled"
                elif expired:
                    state = "timed_out"
                elif aggregate["succeeded"] or (aggregate["blocked"] and not aggregate["failed"]):
                    state = "partial" if aggregate["succeeded"] and aggregate["failed"] else "completed"
                else:
                    state = "failed"
                await conn.execute(
                    """UPDATE crawl_jobs
                       SET state = $2, finished_at = now(),
                           terminal_reason = CASE WHEN $2 = 'timed_out' THEN 'deadline' ELSE terminal_reason END
                       WHERE id = $1""",
                    job["id"], state,
                )
                await conn.execute(
                    """UPDATE scrape_runs SET status = $2, finished_at = now()
                       WHERE id = $1""",
                    job["run_id"], state,
                )
                await _event(conn, job["id"], None, "job_" + state, {})
                finalized += 1
    return finalized


async def _fenced_task(conn: Any, task_id: UUID, lease_token: UUID) -> Any:
    return await conn.fetchrow(
        """SELECT t.*, j.cancel_requested_at, j.deadline_at,
                  j.deadline_at <= now() AS deadline_expired,
                  j.config AS job_config, j.max_pages, j.discovered_count,
                  j.next_discovery_seq
           FROM crawl_tasks t JOIN crawl_jobs j ON j.id = t.job_id
           WHERE t.id = $1 AND t.state = 'leased' AND t.lease_token = $2
           FOR UPDATE OF t, j""",
        task_id, lease_token,
    )


async def reserve_acquisition_attempt(
    task_id: UUID,
    lease_token: UUID,
    route: str,
    reserved_cost: Mapping[str, int | float],
) -> Optional[ReservedAcquisitionAttempt]:
    """Atomically reserve one provider call while a task lease remains current."""
    provider, cost = _native_cost(route, reserved_cost)
    pool = await require_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            task = await _fenced_task(conn, task_id, lease_token)
            if task is None or await _reject_inactive_lease(conn, task):
                return None
            config = _loads(task["job_config"])
            acquisition = config.get("acquisition") or {}
            total_cap = min(4, int(acquisition.get("maxAttempts", 4)))
            total_attempts = await conn.fetchval(
                """SELECT count(*) FROM acquisition_attempts
                   WHERE task_id = $1 AND lease_token IS NOT NULL""",
                task_id,
            )
            route_attempts = await conn.fetchval(
                """SELECT count(*) FROM acquisition_attempts
                   WHERE task_id = $1 AND route = $2 AND lease_token IS NOT NULL""",
                task_id, route,
            )
            if total_attempts >= total_cap or route_attempts >= min(2, total_cap):
                return None

            meters = sorted(cost)
            usage_rows = await conn.fetch(
                """SELECT meter, limit_value, reserved_value, consumed_value
                   FROM crawl_provider_usage
                   WHERE job_id = $1 AND provider = $2 AND meter = ANY($3::TEXT[])
                   ORDER BY meter FOR UPDATE""",
                task["job_id"], provider, meters,
            )
            if {row["meter"] for row in usage_rows} != set(meters):
                return None
            for usage in usage_rows:
                amount = Decimal(str(cost[usage["meter"]]))
                remaining = (
                    usage["limit_value"] - usage["reserved_value"]
                    - usage["consumed_value"]
                )
                if amount > remaining:
                    return None
            for usage in usage_rows:
                await conn.execute(
                    """UPDATE crawl_provider_usage
                       SET reserved_value = reserved_value + $4
                       WHERE job_id = $1 AND provider = $2 AND meter = $3""",
                    task["job_id"], provider, usage["meter"],
                    Decimal(str(cost[usage["meter"]])),
                )
            # Keep legacy worker-attempt numbers (1..max_attempts) intact.
            # Provider route attempts are append-only subattempts of that lease.
            attempt_number = task["attempt_count"] * 1000 + total_attempts + 1
            attempt_id = uuid.uuid4()
            await conn.execute(
                """INSERT INTO acquisition_attempts
                   (id, job_id, task_id, attempt_number, route, provider, lease_token,
                    reserved_cost)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)""",
                attempt_id, task["job_id"], task_id, attempt_number, route,
                provider, lease_token, json.dumps(cost),
            )
            return ReservedAcquisitionAttempt(
                id=attempt_id,
                task_id=task_id,
                route=route,
                provider=provider,
                reserved_cost=cost,
            )


async def finish_acquisition_attempt(
    attempt_id: UUID,
    lease_token: UUID,
    outcome: str,
    actual_cost: Mapping[str, int | float],
    *,
    cost_estimated: bool = False,
) -> bool:
    """Finalize a reserved provider call once, converting only actual usage."""
    pool = await require_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            attempt = await conn.fetchrow(
                "SELECT * FROM acquisition_attempts WHERE id = $1 FOR UPDATE", attempt_id,
            )
            if attempt is None or attempt["lease_token"] != lease_token:
                return False
            task = await _fenced_task(conn, attempt["task_id"], lease_token)
            if task is None or await _reject_inactive_lease(conn, task):
                return False
            if attempt["finished_at"] is not None:
                return False
            provider, actual = _native_cost(attempt["route"], actual_cost)
            reserved = _loads(attempt["reserved_cost"])
            reserved_provider, reserved_cost = _native_cost(attempt["route"], reserved)
            if provider != attempt["provider"] or reserved_provider != provider:
                raise ProviderProtocolError("provider attempt route does not match its ledger")
            for meter, value in actual.items():
                if Decimal(str(value)) > Decimal(str(reserved_cost[meter])):
                    raise ProviderProtocolError("provider reported more native usage than reserved")

            meters = sorted(actual)
            usage_rows = await conn.fetch(
                """SELECT meter FROM crawl_provider_usage
                   WHERE job_id = $1 AND provider = $2 AND meter = ANY($3::TEXT[])
                   ORDER BY meter FOR UPDATE""",
                task["job_id"], provider, meters,
            )
            if {row["meter"] for row in usage_rows} != set(meters):
                raise ProviderProtocolError("provider usage meter is missing")
            for meter in meters:
                reserved_value = Decimal(str(reserved_cost[meter]))
                actual_value = Decimal(str(actual[meter]))
                await conn.execute(
                    """UPDATE crawl_provider_usage
                       SET reserved_value = reserved_value - $4,
                           consumed_value = consumed_value + $5
                       WHERE job_id = $1 AND provider = $2 AND meter = $3""",
                    task["job_id"], provider, meter, reserved_value, actual_value,
                )
            await conn.execute(
                """UPDATE acquisition_attempts
                   SET finished_at = now(),
                       duration_ms = EXTRACT(EPOCH FROM now() - started_at) * 1000,
                       actual_cost = $2::jsonb, outcome = $3, cost_estimated = $4
                   WHERE id = $1""",
                attempt_id, json.dumps(actual), outcome, cost_estimated,
            )
            return True


def _response_cap(url: str) -> int:
    path = url.lower().split("?", 1)[0]
    if path.endswith(DOCUMENT_SUFFIXES):
        return DOCUMENT_RESPONSE_CAP
    if path.endswith(IMAGE_SUFFIXES):
        return IMAGE_RESPONSE_CAP
    return HTML_RESPONSE_CAP


async def _release_lease(conn: Any, task: Any) -> None:
    await conn.execute(
        """UPDATE crawl_jobs
           SET reserved_bytes = GREATEST(0, reserved_bytes - $2),
               reserved_artifact_bytes = GREATEST(0, reserved_artifact_bytes - $3)
           WHERE id = $1""",
        task["job_id"], task["byte_budget_reserved"],
        task["artifact_budget_reserved"],
    )
    await conn.execute(
        "DELETE FROM crawl_origin_leases WHERE task_id = $1 AND lease_token = $2",
        task["id"], task["lease_token"],
    )


def _actual_bytes(task: Any, metadata: Optional[dict]) -> int:
    try:
        value = int((metadata or {}).get("downloaded_bytes", 0))
    except (TypeError, ValueError):
        value = 0
    return min(task["byte_budget_reserved"], max(0, value))


async def _finish_attempt(
    conn: Any, task: Any, outcome: str, *, metadata: Optional[dict] = None,
    downloaded_bytes: int = 0, artifact_bytes: int = 0,
) -> None:
    metadata = metadata or {}
    actual_cost = {
        "downloaded_bytes": downloaded_bytes,
        "artifact_bytes": artifact_bytes,
    }
    await conn.execute(
        """UPDATE acquisition_attempts
           SET route = COALESCE($3, route), provider = COALESCE($4, provider),
               finished_at = now(), duration_ms = EXTRACT(EPOCH FROM now() - started_at) * 1000,
               actual_cost = $5::jsonb, outcome = $6, error_code = $7
           WHERE task_id = $1 AND attempt_number = $2""",
        task["id"], task["attempt_count"], metadata.get("engine"),
        metadata.get("provider"), json.dumps(actual_cost), outcome,
        metadata.get("reason") or metadata.get("error_code"),
    )


async def _charge_download(conn: Any, task: Any, downloaded_bytes: int) -> None:
    if downloaded_bytes:
        await conn.execute(
            "UPDATE crawl_jobs SET downloaded_bytes = downloaded_bytes + $2 WHERE id = $1",
            task["job_id"], downloaded_bytes,
        )


async def _drain_unleased(conn: Any, job_id: UUID, reason: str) -> int:
    """Terminalize queued work after a job-wide hard stop."""
    drained = await conn.fetch(
        """UPDATE crawl_tasks
           SET state = 'permanent_failed', error_class = 'budget', error_code = $2,
               lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
               byte_budget_reserved = 0, artifact_budget_reserved = 0,
               updated_at = now(), finished_at = now()
           WHERE job_id = $1 AND state IN ('pending', 'retry_wait', 'waiting_input')
           RETURNING id""",
        job_id, reason,
    )
    if drained:
        await conn.execute(
            """UPDATE crawl_jobs
               SET terminal_count = terminal_count + $2,
                   failed_count = failed_count + $2,
                   terminal_reason = $3
               WHERE id = $1""",
            job_id, len(drained), reason,
        )
        await _event(conn, job_id, None, "job_" + reason, {"tasks": len(drained)})
    return len(drained)


async def _enforce_failure_limit(conn: Any, task: Any) -> None:
    job = await conn.fetchrow(
        "SELECT failed_count, config FROM crawl_jobs WHERE id = $1 FOR UPDATE",
        task["job_id"],
    )
    config = _loads(job["config"])
    if job["failed_count"] >= int(config.get("maxFailures", 100)):
        await _drain_unleased(conn, task["job_id"], "max_failures_exceeded")


async def _terminalize(
    conn: Any,
    task: Any,
    state: str,
    *,
    error_class: Optional[str] = None,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    await conn.execute(
        """UPDATE crawl_tasks
           SET state = $3, error_class = $4, error_code = $5, error_message = $6,
               lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
               byte_budget_reserved = 0, artifact_budget_reserved = 0,
               updated_at = now(), finished_at = now()
           WHERE id = $1 AND lease_token = $2""",
        task["id"], task["lease_token"], state, error_class, error_code,
        error_message,
    )
    await _release_lease(conn, task)
    job = await conn.fetchrow(
        """UPDATE crawl_jobs j
           SET terminal_count = terminal_count + 1,
               failed_count = failed_count + CASE WHEN $2 = 'permanent_failed' THEN 1 ELSE 0 END,
               state = CASE
                   WHEN cancel_requested_at IS NOT NULL AND NOT EXISTS (
                       SELECT 1 FROM crawl_tasks t
                       WHERE t.job_id = j.id AND t.state = 'leased'
                   ) THEN 'cancelled'
                   ELSE state
               END,
               finished_at = CASE
                   WHEN cancel_requested_at IS NOT NULL AND NOT EXISTS (
                       SELECT 1 FROM crawl_tasks t
                       WHERE t.job_id = j.id AND t.state = 'leased'
                   ) THEN now()
                   ELSE finished_at
               END
           WHERE id = $1
           RETURNING state, run_id""",
        task["job_id"], state,
    )
    if job is not None and job["state"] == "cancelled":
        await conn.execute(
            "UPDATE scrape_runs SET status = 'cancelled', finished_at = now() WHERE id = $1",
            job["run_id"],
        )
    if state == "permanent_failed":
        await _enforce_failure_limit(conn, task)
    await _finish_attempt(conn, task, state, metadata={"error_code": error_code})
    await _event(conn, task["job_id"], task["id"], "task_" + state, {
        "error_code": error_code,
    })


async def _reject_inactive_lease(conn: Any, task: Any) -> bool:
    if task["cancel_requested_at"] is not None:
        await _terminalize(conn, task, "cancelled")
        return True
    if task["deadline_expired"]:
        await _terminalize(
            conn, task, "permanent_failed", error_class="policy",
            error_code="deadline_exceeded",
        )
        return True
    return False


async def claim_task(
    worker_id: str, capabilities: set[str]
) -> Optional[ClaimedTask]:
    """Atomically lease one eligible task without overspending job budgets."""
    pool = await require_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            task = await conn.fetchrow(
                """SELECT t.*
                   FROM crawl_tasks t JOIN crawl_jobs j ON j.id = t.job_id
                   WHERE t.state IN ('pending','retry_wait')
                     AND t.available_at <= now()
                     AND t.required_capabilities <@ $1::TEXT[]
                     AND j.cancel_requested_at IS NULL
                     AND j.deadline_at > now()
                   ORDER BY t.priority, t.available_at, t.discovery_seq
                   FOR UPDATE OF t SKIP LOCKED
                   LIMIT 1""",
                sorted(capabilities),
            )
            if task is None:
                return None
            job = await conn.fetchrow(
                "SELECT * FROM crawl_jobs WHERE id = $1 FOR UPDATE", task["job_id"]
            )
            if job["cancel_requested_at"] is not None or job["deadline_at"] <= await conn.fetchval("SELECT now()"):
                return None
            await conn.execute(
                "INSERT INTO crawl_origins (origin_key) VALUES ($1) ON CONFLICT DO NOTHING",
                task["origin_key"],
            )
            origin = await conn.fetchrow(
                "SELECT * FROM crawl_origins WHERE origin_key = $1 FOR UPDATE",
                task["origin_key"],
            )
            now = await conn.fetchval("SELECT now()")
            blocked_until = origin["cooldown_until"]
            if origin["circuit_state"] == "open":
                candidates = [value for value in (
                    blocked_until, origin["circuit_open_until"],
                ) if value is not None]
                blocked_until = max(candidates) if candidates else None
            if blocked_until is not None and blocked_until > now:
                await conn.execute(
                    "UPDATE crawl_tasks SET available_at = $2, updated_at = now() WHERE id = $1",
                    task["id"], blocked_until,
                )
                return None
            if origin["circuit_state"] == "open":
                await conn.execute(
                    "UPDATE crawl_origins SET circuit_state = 'half_open', updated_at = now() WHERE origin_key = $1",
                    task["origin_key"],
                )
            if origin["next_request_at"] > await conn.fetchval("SELECT now()"):
                await conn.execute(
                    "UPDATE crawl_tasks SET available_at = $2, updated_at = now() WHERE id = $1",
                    task["id"], origin["next_request_at"],
                )
                return None
            if await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM crawl_origin_leases WHERE origin_key = $1)",
                task["origin_key"],
            ):
                return None
            remaining_bytes = job["max_bytes"] - job["downloaded_bytes"] - job["reserved_bytes"]
            remaining_artifacts = (
                job["max_artifact_bytes"] - job["artifact_bytes"]
                - job["reserved_artifact_bytes"]
            )
            byte_allowance = min(remaining_bytes, _response_cap(task["normalized_url"]))
            artifact_allowance = min(remaining_artifacts, INLINE_ARTIFACT_CAP)
            if byte_allowance <= 0 or artifact_allowance <= 0:
                await _drain_unleased(
                    conn, task["job_id"],
                    "byte_budget_exhausted" if byte_allowance <= 0
                    else "artifact_budget_exhausted",
                )
                return None
            token = uuid.uuid4()
            config = _loads(job["config"])
            await conn.execute(
                """UPDATE crawl_jobs
                   SET reserved_bytes = reserved_bytes + $2,
                       reserved_artifact_bytes = reserved_artifact_bytes + $3,
                       state = CASE WHEN state = 'pending' THEN 'running' ELSE state END,
                       started_at = COALESCE(started_at, now())
                   WHERE id = $1""",
                task["job_id"], byte_allowance, artifact_allowance,
            )
            attempt = await conn.fetchval(
                """UPDATE crawl_tasks
                   SET state = 'leased', lease_owner = $2, lease_token = $3,
                       lease_expires_at = now() + ($4 * interval '1 second'),
                       attempt_count = attempt_count + 1,
                       byte_budget_reserved = $5, artifact_budget_reserved = $6,
                       updated_at = now(), started_at = COALESCE(started_at, now())
                   WHERE id = $1 RETURNING attempt_count""",
                task["id"], worker_id, token, LEASE_SECONDS, byte_allowance,
                artifact_allowance,
            )
            await conn.execute(
                """INSERT INTO acquisition_attempts
                   (id, job_id, task_id, attempt_number, route, provider, worker_id,
                    reserved_cost)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)""",
                uuid.uuid4(), task["job_id"], task["id"], attempt,
                config.get("engine", "auto"),
                (config.get("acquisition") or {}).get("provider"), worker_id,
                json.dumps({"downloaded_bytes": byte_allowance,
                            "artifact_bytes": artifact_allowance}),
            )
            await conn.execute(
                """INSERT INTO crawl_origin_leases (origin_key, task_id, lease_token, expires_at)
                   VALUES ($1, $2, $3, now() + ($4 * interval '1 second'))""",
                task["origin_key"], task["id"], token, LEASE_SECONDS,
            )
            await conn.execute(
                """UPDATE crawl_origins
                   SET next_request_at = now() + ($2 * interval '1 millisecond'),
                       updated_at = now()
                   WHERE origin_key = $1""",
                task["origin_key"], int(config.get("minDelayMs", 1000)),
            )
            await _event(conn, task["job_id"], task["id"], "task_claimed", {})
            return ClaimedTask(
                id=task["id"], job_id=task["job_id"], url=task["original_url"],
                normalized_url=task["normalized_url"], origin_key=task["origin_key"],
                depth=task["depth"], attempt=attempt, lease_token=token,
                deadline_at=job["deadline_at"],
                config=config, byte_allowance=byte_allowance,
                artifact_allowance=artifact_allowance,
                required_capabilities=frozenset(task["required_capabilities"]),
            )


async def _insert_discovered(conn: Any, task: Any, urls: tuple[str, ...]) -> int:
    config = _loads(task["job_config"])
    depth = task["depth"] + 1
    if depth > int(config.get("maxDepth", 3)):
        return 0
    remaining = task["max_pages"] - task["discovered_count"]
    if remaining <= 0:
        return 0
    sequence = task["next_discovery_seq"]
    accepted = 0
    capabilities = ["browser"] if config.get("engine") == "browser" else ["http"]
    for original in urls:
        if accepted >= remaining:
            break
        normalized = normalize.normalize_url(original)
        discovered_origin = normalize.origin_key(normalized)
        if not normalized or discovered_origin != task["origin_key"]:
            continue
        inserted = await conn.fetchval(
            """INSERT INTO crawl_tasks
               (id, job_id, original_url, normalized_url, url_hash, origin_key,
                depth, discovery_seq, priority, discovered_from_task_id, state,
                max_attempts, required_capabilities)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $7, $9, 'pending',
                       $10, $11::TEXT[])
               ON CONFLICT (job_id, url_hash) DO NOTHING
               RETURNING id""",
            uuid.uuid4(), task["job_id"], original, normalized,
            hashlib.sha256(normalized.encode("utf-8")).digest(), discovered_origin,
            depth, sequence, task["id"], task["max_attempts"], capabilities,
        )
        if inserted is None:
            continue
        accepted += 1
        sequence += 1
    if accepted:
        await conn.execute(
            """UPDATE crawl_jobs
               SET discovered_count = discovered_count + $2,
                   next_discovery_seq = $3
               WHERE id = $1""",
            task["job_id"], accepted, sequence,
        )
    return accepted


async def heartbeat(task_id: UUID, lease_token: UUID) -> bool:
    pool = await require_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            task = await _fenced_task(conn, task_id, lease_token)
            if task is None or await _reject_inactive_lease(conn, task):
                return False
            await conn.execute(
                """UPDATE crawl_tasks SET lease_expires_at = now() + ($3 * interval '1 second'),
                   updated_at = now() WHERE id = $1 AND lease_token = $2""",
                task_id, lease_token, LEASE_SECONDS,
            )
            lease = await conn.fetchrow(
                """UPDATE crawl_origin_leases
                   SET expires_at = now() + ($3 * interval '1 second')
                   WHERE task_id = $1 AND lease_token = $2 RETURNING task_id""",
                task_id, lease_token, LEASE_SECONDS,
            )
            return lease is not None


async def reserve_browser_navigation(task_id: UUID, lease_token: UUID) -> bool:
    pool = await require_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            task = await _fenced_task(conn, task_id, lease_token)
            if task is None or await _reject_inactive_lease(conn, task):
                return False
            reserved = await conn.fetchrow(
                """UPDATE crawl_jobs
                   SET browser_page_count = browser_page_count + 1
                   WHERE id = $1
                     AND browser_page_count < (config ->> 'maxBrowserPages')::INTEGER
                   RETURNING id""",
                task["job_id"],
            )
            return reserved is not None


async def complete_task(task_id: UUID, lease_token: UUID, result: TaskResult) -> bool:
    pool = await require_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            task = await _fenced_task(conn, task_id, lease_token)
            if task is None or await _reject_inactive_lease(conn, task):
                return False
            metadata = dict(result.metadata)
            downloaded = _actual_bytes(task, metadata)
            try:
                reported_downloaded = max(0, int(metadata.get("downloaded_bytes", 0)))
                screenshot_bytes = max(0, int(metadata.get("screenshot_bytes", 0)))
            except (TypeError, ValueError):
                reported_downloaded = screenshot_bytes = 0
            artifact_bytes = len(result.markdown.encode("utf-8")) + max(
                0, screenshot_bytes
            )
            if (reported_downloaded > task["byte_budget_reserved"]
                    or artifact_bytes > task["artifact_budget_reserved"]):
                reason = ("byte_budget_exhausted" if reported_downloaded > task["byte_budget_reserved"]
                          else "artifact_budget_exhausted")
                await _terminalize(
                    conn, task, "permanent_failed", error_class="budget", error_code=reason,
                )
                await _charge_download(conn, task, downloaded)
                await _finish_attempt(
                    conn, task, "failed", metadata=metadata,
                    downloaded_bytes=downloaded,
                )
                await _drain_unleased(conn, task["job_id"], reason)
                return False
            result_id = uuid.uuid4()
            stored = await conn.fetchval(
                """INSERT INTO crawl_results
                   (id, task_id, final_url, status_code, title, markdown, metadata,
                    content_sha256, downloaded_bytes, artifact_bytes)
                   VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10)
                   ON CONFLICT (task_id) DO NOTHING RETURNING id""",
                result_id, task_id, result.final_url, result.status_code, result.title,
                result.markdown, json.dumps(metadata),
                hashlib.sha256(result.markdown.encode("utf-8")).hexdigest(), downloaded,
                artifact_bytes,
            )
            if stored is None:
                return False
            await _insert_discovered(conn, task, result.discovered_urls)
            await conn.execute(
                """UPDATE crawl_tasks
                   SET state = 'succeeded', result_id = $3, lease_owner = NULL,
                       lease_token = NULL, lease_expires_at = NULL,
                       byte_budget_reserved = 0, artifact_budget_reserved = 0,
                       updated_at = now(), finished_at = now()
                   WHERE id = $1 AND lease_token = $2""",
                task_id, lease_token, stored,
            )
            await _release_lease(conn, task)
            await conn.execute(
                "UPDATE crawl_origins SET consecutive_failures = 0, circuit_state = 'closed', "
                "circuit_open_until = NULL, cooldown_until = NULL, updated_at = now() "
                "WHERE origin_key = $1",
                task["origin_key"],
            )
            await conn.execute(
                """UPDATE crawl_jobs
                   SET downloaded_bytes = downloaded_bytes + $2,
                       artifact_bytes = artifact_bytes + $3,
                       terminal_count = terminal_count + 1,
                       succeeded_count = succeeded_count + 1
                   WHERE id = $1""",
                task["job_id"], downloaded, artifact_bytes,
            )
            await _finish_attempt(
                conn, task, "succeeded", metadata=metadata,
                downloaded_bytes=downloaded, artifact_bytes=artifact_bytes,
            )
            await _event(conn, task["job_id"], task_id, "task_succeeded", {})
            return True


async def retry_task(
    task_id: UUID, lease_token: UUID, decision: "FailureDecision", available_at: datetime,
    metadata: Optional[dict] = None,
) -> bool:
    pool = await require_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            task = await _fenced_task(conn, task_id, lease_token)
            if task is None or await _reject_inactive_lease(conn, task):
                return False
            downloaded = _actual_bytes(task, metadata)
            if task["attempt_count"] >= task["max_attempts"]:
                await _terminalize(
                    conn, task, "permanent_failed", error_class=decision.error_class,
                    error_code=decision.error_code,
                )
                await _charge_download(conn, task, downloaded)
                await _finish_attempt(
                    conn, task, "failed", metadata=metadata,
                    downloaded_bytes=downloaded,
                )
                return True
            await conn.execute(
                """UPDATE crawl_tasks
                   SET state = 'retry_wait', available_at = $3,
                       error_class = $4, error_code = $5, lease_owner = NULL,
                       lease_token = NULL, lease_expires_at = NULL,
                       byte_budget_reserved = 0, artifact_budget_reserved = 0,
                       updated_at = now()
                   WHERE id = $1 AND lease_token = $2""",
                task_id, lease_token, available_at, decision.error_class,
                decision.error_code,
            )
            await _release_lease(conn, task)
            await _charge_download(conn, task, downloaded)
            await conn.execute(
                """UPDATE crawl_origins
                   SET next_request_at = GREATEST(next_request_at, $2),
                       consecutive_failures = consecutive_failures +
                           CASE WHEN $3 = 'transport' OR $3 = 'http' THEN 1 ELSE 0 END,
                       circuit_state = CASE WHEN (consecutive_failures +
                           CASE WHEN $3 = 'transport' OR $3 = 'http' THEN 1 ELSE 0 END) >= 5
                           THEN 'open' ELSE circuit_state END,
                       circuit_open_until = CASE WHEN (consecutive_failures +
                           CASE WHEN $3 = 'transport' OR $3 = 'http' THEN 1 ELSE 0 END) >= 5
                           THEN now() + interval '300 seconds' ELSE circuit_open_until END,
                       cooldown_until = GREATEST(cooldown_until, $2), updated_at = now()
                   WHERE origin_key = $1""",
                task["origin_key"], available_at, decision.error_class,
            )
            await _event(conn, task["job_id"], task_id, "task_retry_wait", {
                "error_code": decision.error_code,
            })
            await _finish_attempt(
                conn, task, "retry", metadata=metadata,
                downloaded_bytes=downloaded,
            )
            return True


async def fail_task(
    task_id: UUID, lease_token: UUID, decision: "FailureDecision",
    metadata: Optional[dict] = None,
) -> bool:
    pool = await require_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            task = await _fenced_task(conn, task_id, lease_token)
            if task is None or await _reject_inactive_lease(conn, task):
                return False
            downloaded = _actual_bytes(task, metadata)
            await _terminalize(
                conn, task, "permanent_failed", error_class=decision.error_class,
                error_code=decision.error_code,
            )
            await _charge_download(conn, task, downloaded)
            await _finish_attempt(
                conn, task, "failed", metadata=metadata,
                downloaded_bytes=downloaded,
            )
            return True


async def robots_cache(origin_key: str) -> Optional[dict]:
    pool = await require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM crawl_origins WHERE origin_key = $1", origin_key)
    return dict(row) if row is not None else None


async def store_robots(origin_key: str, body: str, status: int) -> None:
    pool = await require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE crawl_origins SET robots_body = $2, robots_status = $3,
               robots_fetched_at = now(), robots_expires_at = now() + interval '24 hours',
               updated_at = now() WHERE origin_key = $1""",
            origin_key, body, status,
        )


async def block_robots(task_id: UUID, lease_token: UUID, code: str) -> bool:
    pool = await require_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            task = await _fenced_task(conn, task_id, lease_token)
            if task is None or await _reject_inactive_lease(conn, task):
                return False
            await _terminalize(conn, task, "blocked_robots", error_class="policy", error_code=code)
            return True


async def reap_expired_leases() -> int:
    pool = await require_pool()
    reaped = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            expired = await conn.fetch(
                """SELECT id, lease_token FROM crawl_tasks
                   WHERE state = 'leased' AND lease_expires_at <= now()
                   FOR UPDATE SKIP LOCKED"""
            )
            for lease in expired:
                task = await _fenced_task(conn, lease["id"], lease["lease_token"])
                if task is None:
                    continue
                if await _reject_inactive_lease(conn, task):
                    reaped += 1
                    continue
                if task["attempt_count"] >= task["max_attempts"]:
                    await _terminalize(
                        conn, task, "permanent_failed", error_class="transport",
                        error_code="lease_expired",
                    )
                else:
                    await conn.execute(
                        """UPDATE crawl_tasks
                           SET state = 'retry_wait', available_at = now(),
                               error_class = 'transport', error_code = 'lease_expired',
                               lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                               byte_budget_reserved = 0, artifact_budget_reserved = 0,
                               updated_at = now()
                           WHERE id = $1 AND lease_token = $2""",
                        task["id"], task["lease_token"],
                    )
                    await _release_lease(conn, task)
                    await _finish_attempt(
                        conn, task, "retry",
                        metadata={"error_code": "lease_expired"},
                    )
                    await _event(conn, task["job_id"], task["id"], "task_retry_wait", {
                        "error_code": "lease_expired",
                    })
                reaped += 1
    return reaped
