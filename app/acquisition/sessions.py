"""Durable, provider-neutral human intervention sessions."""
import base64
import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Protocol
from uuid import UUID, uuid4

from app.crawl.types import ClaimedTask, TaskResult
from app.db.pool import get_pool


_BACKEND_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SCOPES = frozenset({"view", "control", "worker"})


class SessionPersistenceUnavailable(RuntimeError):
    pass


class SessionStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class SessionHandle:
    id: UUID
    backend: str
    expires_at: datetime


@dataclass(frozen=True)
class SessionSnapshot:
    status: str
    expires_at: datetime
    usage: Mapping[str, float | int]


class SessionBackend(Protocol):
    async def create(self, handle: SessionHandle, target_url: str,
                     profile_state: bytes | None) -> SessionSnapshot:
        ...

    async def inspect(self, handle: SessionHandle) -> SessionSnapshot:
        ...

    async def send(self, handle: SessionHandle, action: Mapping[str, object]) -> object:
        ...

    async def resume(self, handle: SessionHandle) -> TaskResult:
        ...

    async def close(self, handle: SessionHandle) -> None:
        ...


async def _require_pool(pool=None):
    resolved = pool or await get_pool()
    if resolved is None:
        raise SessionPersistenceUnavailable("live sessions require PostgreSQL")
    return resolved


def _validate_ttl(ttl_seconds: int) -> None:
    if not 300 <= ttl_seconds <= 3600:
        raise ValueError("session TTL must be between 300 and 3600 seconds")


async def wait_for_input(
    claim: ClaimedTask, *, backend: str, worker_id: str, ttl_seconds: int = 900, pool=None,
) -> SessionHandle:
    """Atomically exchange an active worker lease for a bounded human session."""
    _validate_ttl(ttl_seconds)
    if not _BACKEND_RE.fullmatch(backend) or not worker_id:
        raise ValueError("invalid session backend or worker ID")
    resolved = await _require_pool(pool)
    try:
        async with resolved.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM worker_api.start_live_session($1,$2,$3,$4,$5,$6)",
                claim.id, claim.lease_token, backend, worker_id, ttl_seconds,
                "human_input_required",
            )
    except Exception as exc:
        if getattr(exc, "sqlstate", None) in {"22023", "42501"}:
            raise SessionStateError("task lease is no longer active for this worker") from exc
        raise
    if row is None:
        raise SessionStateError("task lease is no longer active for this worker")
    return SessionHandle(id=row["id"], backend=row["backend"], expires_at=row["expires_at"])


async def wait_for_input_local(
    claim: ClaimedTask, *, backend: str, worker_id: str, ttl_seconds: int = 900, pool=None,
) -> SessionHandle:
    """Core-role variant of the same fenced transition used by remote workers."""
    _validate_ttl(ttl_seconds)
    if not _BACKEND_RE.fullmatch(backend) or not worker_id:
        raise ValueError("invalid session backend or worker ID")
    resolved = await _require_pool(pool)
    async with resolved.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """SELECT t.id, t.job_id, t.byte_budget_reserved,
                          t.artifact_budget_reserved, j.deadline_at
                   FROM crawl_tasks t JOIN crawl_jobs j ON j.id = t.job_id
                   WHERE t.id = $1 AND t.state = 'leased' AND t.lease_token = $2
                     AND t.lease_owner = $3 AND j.cancel_requested_at IS NULL
                     AND j.deadline_at > now()
                   FOR UPDATE OF t, j""",
                claim.id, claim.lease_token, worker_id,
            )
            if row is None:
                raise SessionStateError("task lease is no longer active for this worker")
            expires_at = await conn.fetchval(
                "SELECT LEAST(now() + ($1 * interval '1 second'), $2::timestamptz)",
                ttl_seconds, row["deadline_at"],
            )
            session_id = uuid4()
            await conn.execute(
                """INSERT INTO live_sessions
                       (id, task_id, backend, worker_id, state, expires_at)
                   VALUES ($1,$2,$3,NULL,'waiting',$4)""",
                session_id, claim.id, backend, expires_at,
            )
            await conn.execute(
                """UPDATE crawl_tasks SET state = 'waiting_input',
                       error_code = 'human_input_required', lease_owner = NULL,
                       lease_token = NULL, lease_expires_at = NULL,
                       byte_budget_reserved = 0, artifact_budget_reserved = 0,
                       updated_at = now() WHERE id = $1""",
                claim.id,
            )
            await conn.execute(
                "DELETE FROM crawl_origin_leases WHERE task_id = $1 AND lease_token = $2",
                claim.id, claim.lease_token,
            )
            await conn.execute("DELETE FROM proxy_leases WHERE task_id = $1", claim.id)
            await conn.execute(
                """UPDATE crawl_jobs SET
                       reserved_bytes = GREATEST(0, reserved_bytes - $2),
                       reserved_artifact_bytes = GREATEST(0, reserved_artifact_bytes - $3)
                   WHERE id = $1""",
                row["job_id"], row["byte_budget_reserved"], row["artifact_budget_reserved"],
            )
            await conn.execute(
                """UPDATE acquisition_attempts SET finished_at = now(),
                       outcome = 'waiting_input', actual_cost = '{}'
                   WHERE task_id = $1 AND attempt_number = $2""",
                claim.id, claim.attempt,
            )
            await conn.execute(
                   """INSERT INTO crawl_events(job_id, task_id, event, metadata)
                   VALUES ($1,$2,'task_waiting_input',
                           jsonb_build_object('session_id',$3::UUID,'backend',$4::TEXT))""",
                row["job_id"], claim.id, session_id, backend,
            )
    return SessionHandle(id=session_id, backend=backend, expires_at=expires_at)


async def issue_token(session_id: UUID, scope: str, *, ttl_seconds: int = 60, pool=None) -> str:
    """Return a fresh one-use bearer token; only its SHA-256 digest is stored."""
    if scope not in _SCOPES or not 1 <= ttl_seconds <= 3600:
        raise ValueError("invalid token scope or TTL")
    resolved = await _require_pool(pool)
    raw = secrets.token_bytes(32)
    token = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(raw).digest()
    async with resolved.acquire() as conn:
        async with conn.transaction():
            session = await conn.fetchrow(
                """SELECT expires_at FROM live_sessions
                   WHERE id = $1 AND state IN ('waiting','connected') AND expires_at > now()
                   FOR UPDATE""",
                session_id,
            )
            if session is None:
                raise SessionStateError("session is not available")
            expires_at = min(session["expires_at"], await conn.fetchval(
                "SELECT now() + ($1 * interval '1 second')", ttl_seconds
            ))
            await conn.execute(
                """INSERT INTO live_session_tokens (id, session_id, scope, token_hash, expires_at)
                   VALUES ($1, $2, $3, $4, $5)""",
                uuid4(), session_id, scope, digest, expires_at,
            )
    return token


async def consume_token(session_id: UUID, token: str, scope: str, *, pool=None) -> bool:
    """Atomically consume a matching unexpired token exactly once."""
    if scope not in _SCOPES:
        return False
    try:
        raw = base64.b64decode(
            token + "=" * (-len(token) % 4), altchars=b"-_", validate=True,
        )
    except (ValueError, UnicodeEncodeError):
        return False
    if len(raw) != 32:
        return False
    resolved = await _require_pool(pool)
    async with resolved.acquire() as conn:
        consumed = await conn.fetchval(
            """UPDATE live_session_tokens SET consumed_at = now()
               WHERE session_id = $1 AND scope = $2 AND token_hash = $3
                 AND consumed_at IS NULL AND expires_at > now()
               RETURNING TRUE""",
            session_id, scope, hashlib.sha256(raw).digest(),
        )
    return bool(consumed)


async def inspect(session_id: UUID, *, pool=None) -> SessionSnapshot | None:
    resolved = await _require_pool(pool)
    async with resolved.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT state, expires_at FROM live_sessions WHERE id = $1", session_id
        )
    if row is None:
        return None
    status = "expired" if row["expires_at"] <= datetime.now(row["expires_at"].tzinfo) else row["state"]
    return SessionSnapshot(status=status, expires_at=row["expires_at"], usage={})


async def request_resume(session_id: UUID, *, pool=None) -> bool:
    resolved = await _require_pool(pool)
    async with resolved.acquire() as conn:
        return bool(await conn.fetchval(
            """UPDATE live_sessions SET state = 'resuming', last_seen_at = now()
               WHERE id = $1 AND state IN ('waiting','connected') AND expires_at > now()
               RETURNING TRUE""",
            session_id,
        ))


async def close(
    session_id: UUID, reason: str, *, worker_id: str | None = None, pool=None,
) -> bool:
    """Close one session and terminalize any task still owned by it."""
    if reason not in {"cancelled", "expired"}:
        raise ValueError("invalid session close reason")
    resolved = await _require_pool(pool)
    async with resolved.acquire() as conn:
        async with conn.transaction():
            session = await conn.fetchrow(
                """SELECT * FROM live_sessions
                   WHERE id = $1 AND state NOT IN ('closed','expired','cancelled')
                   FOR UPDATE""",
                session_id,
            )
            if session is None:
                return False
            task = await conn.fetchrow(
                "SELECT * FROM crawl_tasks WHERE id = $1 FOR UPDATE",
                session["task_id"],
            )
            if task is not None and task["state"] == "succeeded":
                await conn.execute(
                    """UPDATE live_sessions SET state = 'closed', closed_at = now(),
                           last_seen_at = now() WHERE id = $1""",
                    session_id,
                )
                return True
            terminalized = task is not None and (
                task["state"] == "waiting_input"
                or (
                    task["state"] == "leased"
                    and (reason == "cancelled" or task["lease_owner"] == worker_id)
                )
            )
            if terminalized and task["state"] == "leased":
                await conn.execute(
                    """UPDATE crawl_jobs SET
                           reserved_bytes = GREATEST(0, reserved_bytes - $2),
                           reserved_artifact_bytes = GREATEST(0, reserved_artifact_bytes - $3)
                       WHERE id = $1""",
                    task["job_id"], task["byte_budget_reserved"],
                    task["artifact_budget_reserved"],
                )
                await conn.execute(
                    "DELETE FROM crawl_origin_leases WHERE task_id = $1",
                    task["id"],
                )
                await conn.execute("DELETE FROM proxy_leases WHERE task_id = $1", task["id"])
            if terminalized:
                terminal_state = "cancelled" if reason == "cancelled" else "permanent_failed"
                error_class = "policy" if reason == "cancelled" else "transport"
                error_code = (
                    "human_input_cancelled" if reason == "cancelled"
                    else "human_input_timeout"
                )
                await conn.execute(
                    """UPDATE crawl_tasks SET state = $2, error_class = $3,
                           error_code = $4, lease_owner = NULL, lease_token = NULL,
                           lease_expires_at = NULL, byte_budget_reserved = 0,
                           artifact_budget_reserved = 0, finished_at = now(), updated_at = now()
                       WHERE id = $1""",
                    task["id"], terminal_state, error_class, error_code,
                )
                await conn.execute(
                    """UPDATE acquisition_attempts SET finished_at = COALESCE(finished_at, now()),
                           outcome = $2, error_code = $3
                       WHERE task_id = $1 AND attempt_number = (
                           SELECT attempt_count FROM crawl_tasks WHERE id = $1
                       )""",
                    task["id"], "cancelled" if reason == "cancelled" else "failed",
                    error_code,
                )
                await conn.execute(
                    """UPDATE crawl_jobs SET terminal_count = terminal_count + 1,
                           failed_count = failed_count + $2
                       WHERE id = $1""",
                    task["job_id"], 0 if reason == "cancelled" else 1,
                )
                await conn.execute(
                    """INSERT INTO crawl_events(job_id, task_id, event, metadata)
                       VALUES ($1,$2,$3,jsonb_build_object('reason',$4::TEXT))""",
                    task["job_id"], task["id"],
                    "task_cancelled" if reason == "cancelled" else "task_permanent_failed",
                    error_code,
                )
            await conn.execute(
                """UPDATE live_sessions SET state = $2, closed_at = now(), last_seen_at = now()
                   WHERE id = $1""",
                session_id, reason,
            )
            return True


async def cancel(session_id: UUID, *, pool=None) -> bool:
    """Cancel the session and its parked task in one transaction."""
    return await close(session_id, "cancelled", pool=pool)


async def touch(session_id: UUID, *, pool=None) -> bool:
    resolved = await _require_pool(pool)
    async with resolved.acquire() as conn:
        return bool(await conn.fetchval(
            """UPDATE live_sessions SET state = 'connected', last_seen_at = now()
               WHERE id = $1 AND state IN ('waiting','connected') AND expires_at > now()
               RETURNING TRUE""", session_id,
        ))


async def belongs_to_job(session_id: UUID, job_id: UUID, *, pool=None) -> bool:
    resolved = await _require_pool(pool)
    async with resolved.acquire() as conn:
        return bool(await conn.fetchval(
            """SELECT EXISTS (SELECT 1 FROM live_sessions s JOIN crawl_tasks t ON t.id = s.task_id
               WHERE s.id = $1 AND t.job_id = $2)""", session_id, job_id,
        ))


async def worker_for_artifact(session_id: UUID, *, pool=None) -> str | None:
    resolved = await _require_pool(pool)
    async with resolved.acquire() as conn:
        return await conn.fetchval(
            """SELECT worker_id FROM live_sessions
               WHERE id = $1 AND state IN ('waiting','connected','resuming')
                 AND expires_at > now()""",
            session_id,
        )


async def close_completed(session_id: UUID, *, pool=None) -> bool:
    resolved = await _require_pool(pool)
    async with resolved.acquire() as conn:
        return bool(await conn.fetchval(
            """UPDATE live_sessions SET state = 'closed', closed_at = now(), last_seen_at = now()
               WHERE id = $1 AND state = 'resuming'
               RETURNING TRUE""",
            session_id,
        ))


async def expire_due(*, pool=None) -> int:
    """Terminalize expired parked tasks so no session can strand a job."""
    resolved = await _require_pool(pool)
    expired = 0
    async with resolved.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                """UPDATE live_sessions SET state = 'expired', closed_at = now()
                   WHERE state IN ('starting','waiting','connected','resuming')
                     AND expires_at <= now()
                   RETURNING id, task_id"""
            )
            for row in rows:
                task = await conn.fetchrow(
                    """UPDATE crawl_tasks SET state = 'permanent_failed',
                           error_class = 'transport', error_code = 'human_input_timeout',
                           finished_at = now(), updated_at = now()
                       WHERE id = $1 AND state = 'waiting_input'
                       RETURNING job_id""",
                    row["task_id"],
                )
                if task is None:
                    continue
                expired += 1
                await conn.execute(
                    """UPDATE acquisition_attempts SET finished_at = COALESCE(finished_at, now()),
                           outcome = 'failed', error_code = 'human_input_timeout'
                       WHERE task_id = $1 AND attempt_number = (
                           SELECT attempt_count FROM crawl_tasks WHERE id = $1
                       )""",
                    row["task_id"],
                )
                await conn.execute(
                    """UPDATE crawl_jobs SET terminal_count = terminal_count + 1,
                           failed_count = failed_count + 1 WHERE id = $1""",
                    task["job_id"],
                )
                await conn.execute(
                    """INSERT INTO crawl_events(job_id, task_id, event, metadata)
                       VALUES ($1,$2,'task_failed','{"reason":"human_input_timeout"}')""",
                    task["job_id"], row["task_id"],
                )
    return expired
