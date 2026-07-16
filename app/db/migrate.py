"""Hand-rolled forward-only SQL migrations (no ORM/Alembic — overkill here).

Applies app/db/migrations/NNNN_*.sql in filename order, recording each applied
version in schema_migrations so re-runs are no-ops. Runs on startup whenever
DATABASE_URL is set; safe to call repeatedly (idempotent), which matters because
the deploy pipeline restarts the container on every push to main.
"""
import logging
import os
from typing import List, Tuple

from app.db.pool import database_url

logger = logging.getLogger("db.migrate")

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")


def _migration_files() -> List[Tuple[str, str]]:
    """Return (version, absolute_path) for every *.sql file, sorted by name."""
    if not os.path.isdir(MIGRATIONS_DIR):
        return []
    out = []
    for name in sorted(os.listdir(MIGRATIONS_DIR)):
        if name.endswith(".sql"):
            out.append((name[:-4], os.path.join(MIGRATIONS_DIR, name)))
    return out


async def run_migrations() -> int:
    """Apply any unapplied migrations. Returns the count applied this call.

    No-op (returns 0) when DATABASE_URL is unset. Swallows nothing here — the
    caller (startup hook) wraps this so a migration failure is logged without
    crashing the app, but a clear traceback still surfaces in the logs.
    """
    url = database_url()
    if not url:
        return 0

    import asyncpg  # lazy

    conn = await asyncpg.connect(dsn=url)
    applied = 0
    try:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        done = {
            r["version"]
            for r in await conn.fetch("SELECT version FROM schema_migrations")
        }
        for version, path in _migration_files():
            if version in done:
                continue
            with open(path, encoding="utf-8") as f:
                sql = f.read()
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES ($1)"
                    " ON CONFLICT (version) DO NOTHING",
                    version,
                )
            applied += 1
            logger.info("applied migration %s", version)
    finally:
        await conn.close()
    if applied:
        logger.info("migrations: applied %d new migration(s)", applied)
    return applied
