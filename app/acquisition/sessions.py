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


async def cancel(session_id: UUID, *, pool=None) -> bool:
    """Prevent stale completion; the tunnel backend closes the live browser."""
    resolved = await _require_pool(pool)
    async with resolved.acquire() as conn:
        return bool(await conn.fetchval(
            """UPDATE live_sessions SET state = 'cancelled', closed_at = now()
               WHERE id = $1 AND state NOT IN ('closed','expired','cancelled')
               RETURNING TRUE""",
            session_id,
        ))
