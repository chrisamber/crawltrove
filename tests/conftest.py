"""Test fixtures for the persistence layer.

The no-op tests need no database. The DB-path tests require a reachable Postgres;
conftest auto-discovers a working local connection (TCP or unix socket) and
creates an isolated `crawltrove_test` database. If nothing is reachable, the
DB-path tests skip cleanly (the no-op safety tests still run).

Override discovery with TEST_PG_ADMIN_DSN (a DSN to a maintenance db like
`postgres`) and TEST_DB_NAME.
"""
import asyncio
import os
import re
from urllib.parse import unquote, urlsplit

import pytest
import pytest_asyncio

TEST_DB = os.environ.get("TEST_DB_NAME", "crawltrove_test")


def _safe_test_database_name(name):
    return bool(re.fullmatch(r"(?:test(?:_[a-z0-9_]+)?|[a-z0-9_]+_test)", name or ""))


def _database_name(dsn):
    return unquote(urlsplit(dsn).path.lstrip("/").split("?", 1)[0])


if not _safe_test_database_name(TEST_DB):
    pytest.exit(
        "TEST_DB_NAME must be clearly test-only (test, test_*, or *_test)",
        returncode=2,
    )

# (admin DSN to the maintenance db, DSN to our test db). First admin DSN that
# connects wins.
_CANDIDATES = []
if os.environ.get("TEST_PG_ADMIN_DSN"):
    admin = os.environ["TEST_PG_ADMIN_DSN"]
    test_dsn = (os.environ.get("TEST_DATABASE_URL")
                or f"postgresql://localhost:5432/{TEST_DB}")
    if _database_name(test_dsn) != TEST_DB:
        pytest.exit(
            "TEST_DATABASE_URL database must exactly match TEST_DB_NAME",
            returncode=2,
        )
    _CANDIDATES.append((admin, test_dsn))
_CANDIDATES += [
    (f"postgresql://localhost:5432/postgres", f"postgresql://localhost:5432/{TEST_DB}"),
    (f"postgresql:///postgres?host=/tmp", f"postgresql:///{TEST_DB}?host=/tmp"),
    (f"postgresql:///postgres?host=/var/run/postgresql",
     f"postgresql:///{TEST_DB}?host=/var/run/postgresql"),
]

_RESOLVED = {"checked": False, "test_dsn": None}


async def _ensure_test_db():
    """Find a working admin DSN and CREATE DATABASE crawltrove_test if needed."""
    import asyncpg
    for admin_dsn, test_dsn in _CANDIDATES:
        try:
            conn = await asyncpg.connect(dsn=admin_dsn, timeout=3)
        except Exception:
            continue
        try:
            exists = await conn.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1", TEST_DB)
            if not exists:
                await conn.execute(f'CREATE DATABASE "{TEST_DB}"')
        finally:
            await conn.close()
        return test_dsn
    return None


def _resolve():
    if not _RESOLVED["checked"]:
        _RESOLVED["checked"] = True
        try:
            _RESOLVED["test_dsn"] = asyncio.run(_ensure_test_db())
        except Exception:
            _RESOLVED["test_dsn"] = None
    return _RESOLVED["test_dsn"]


# Evaluated at import for the skip marker.
_TEST_DSN = _resolve()
if os.environ.get("REQUIRE_TEST_DATABASE", "").lower() in ("1", "true", "yes"):
    if _TEST_DSN is None:
        pytest.exit("REQUIRE_TEST_DATABASE is set but Postgres is unavailable", returncode=2)
requires_db = pytest.mark.skipif(
    _TEST_DSN is None, reason="no local Postgres reachable for DB-path tests")


@pytest_asyncio.fixture
async def db(monkeypatch):
    """Enable persistence against the test DB with a clean schema each test."""
    from app.db import pool, migrate

    monkeypatch.setenv("DATABASE_URL", _TEST_DSN)
    await pool.reset_pool()
    await migrate.run_migrations()
    p = await pool.get_pool()
    async with p.acquire() as conn:
        actual_db = await conn.fetchval("SELECT current_database()")
        if actual_db != TEST_DB or not _safe_test_database_name(actual_db):
            pytest.fail(
                f"refusing to truncate non-test database {actual_db!r}",
                pytrace=False,
            )
        await conn.execute(
            "TRUNCATE crawl_jobs, crawl_tasks, crawl_results, crawl_origins,"
            " crawl_origin_leases, crawl_events, acquisition_attempts, research_runs,"
            " scrape_errors, extracted_records, scraped_pages,"
            " scrape_runs, scrape_jobs, workers RESTART IDENTITY CASCADE"
        )
    try:
        yield p
    finally:
        await pool.reset_pool()
