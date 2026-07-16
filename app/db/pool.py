"""Lazy asyncpg connection pool, gated entirely behind DATABASE_URL.

When DATABASE_URL is unset the service behaves exactly as it did before any
persistence existed: get_pool() returns None and every repo call no-ops. asyncpg
is imported lazily *inside* get_pool() so the disabled path does not even require
the dependency to be installed.

Single-process / single-worker only (matching the dedup lock + in-memory crawl
store). Do not add `--workers N` without reworking those.
"""
import asyncio
import logging
import os
from typing import Any, Optional

logger = logging.getLogger("db.pool")

_pool: Optional[Any] = None          # asyncpg.Pool once created
_lock: Optional[asyncio.Lock] = None  # created lazily, bound to the live loop


def database_url() -> Optional[str]:
    """The configured DSN, or None when persistence is disabled."""
    return os.environ.get("DATABASE_URL") or None


def enabled() -> bool:
    return database_url() is not None


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


async def get_pool() -> Optional[Any]:
    """Return the shared pool, creating it on first use.

    Returns None — never raises — when DATABASE_URL is unset OR the database is
    unreachable, so a missing/misconfigured DB degrades to the legacy file-only
    behaviour rather than breaking a scrape.
    """
    global _pool
    url = database_url()
    if not url:
        return None
    if _pool is not None:
        return _pool
    async with _get_lock():
        if _pool is not None:  # another coroutine won the race
            return _pool
        try:
            import asyncpg  # lazy: only needed when persistence is enabled
            _pool = await asyncpg.create_pool(
                dsn=url, min_size=1, max_size=5, command_timeout=30,
            )
        except Exception as e:
            # Leave _pool as None so a later call can retry once the DB recovers.
            logger.warning("DB pool init failed (%s); persistence disabled for now", e)
            _pool = None
    return _pool


async def ping() -> bool:
    """True when the pool is live and the DB answers a trivial query.

    Used by /api/health to distinguish 'up' from 'down' (configured but
    unreachable). Never raises.
    """
    p = await get_pool()
    if p is None:
        return False
    try:
        async with p.acquire() as conn:
            await conn.execute("SELECT 1")
        return True
    except Exception:
        return False


async def reset_pool() -> None:
    """Close and forget the pool (used by tests and on shutdown)."""
    global _pool, _lock
    if _pool is not None:
        try:
            await _pool.close()
        except Exception:
            pass
    _pool = None
    _lock = None
