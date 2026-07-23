"""Exercise the forward-only v0.3-to-v0.4 PostgreSQL upgrade on a disposable DB."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
MIGRATIONS = ROOT / "app" / "db" / "migrations"
BASELINE = ("0001_init.sql", "0002_fts.sql", "0003_research_runs.sql")
UPGRADE = (
    "0004_durable_crawl.sql",
    "0005_remote_workers.sql",
    "0006_artifact_bucket_guard.sql",
    "0007_owned_acquisition.sql",
    "0008_managed_acquisition.sql",
    "0009_proxy_worker_protocol.sql",
    "0010_remote_managed_acquisition.sql",
    "0011_session_worker_protocol.sql",
    "0012_queue_claim_performance.sql",
)
TEST_DATABASE = "crawltrove_migration_compat_test"


def is_compat_database(name: str) -> bool:
    """Only permit the one disposable database this script owns."""
    return name == TEST_DATABASE


def database_dsn(admin_dsn: str, database: str) -> str:
    parsed = urlsplit(admin_dsn)
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{database}", parsed.query, parsed.fragment))


async def apply(conn, names: tuple[str, ...]) -> None:
    for name in names:
        async with conn.transaction():
            await conn.execute((MIGRATIONS / name).read_text(encoding="utf-8"))
            await conn.execute(
                "INSERT INTO schema_migrations (version) VALUES ($1) ON CONFLICT DO NOTHING",
                name.removesuffix(".sql"),
            )


async def assert_v040_migration_runner_accepts_schema(database_dsn: str) -> None:
    """Exercise the same forward-only migration hook used by app startup."""
    from app.db import migrate, pool

    previous_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_dsn
    try:
        applied = await migrate.run_migrations()
        assert applied == 0, f"unexpected migrations applied after compatibility upgrade: {applied}"
    finally:
        await pool.reset_pool()
        if previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_database_url


async def verify(admin_dsn: str, database: str) -> None:
    import asyncpg

    if not is_compat_database(database):
        raise ValueError(f"migration compatibility database must be {TEST_DATABASE!r}")
    quoted = f'"{database}"'
    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {quoted}")
        await admin.execute(f"CREATE DATABASE {quoted}")
    finally:
        await admin.close()

    try:
        conn = await asyncpg.connect(database_dsn(admin_dsn, database))
        try:
            await apply(conn, BASELINE)
            job_id = await conn.fetchval(
                "INSERT INTO scrape_jobs (name, target_url) VALUES ('compat', 'https://example.com') RETURNING id"
            )
            run_id = await conn.fetchval(
                "INSERT INTO scrape_runs (job_id, external_id, status) VALUES ($1, 'compat-run', 'completed') RETURNING id",
                job_id,
            )
            await conn.execute(
                "INSERT INTO scraped_pages (run_id, url, extracted_text) VALUES ($1, 'https://example.com', 'legacy')",
                run_id,
            )
            await apply(conn, UPGRADE)
            await assert_v040_migration_runner_accepts_schema(database_dsn(admin_dsn, database))
            assert await conn.fetchval("SELECT external_id FROM scrape_runs WHERE id = $1", run_id) == "compat-run"
            assert await conn.fetchval("SELECT extracted_text FROM scraped_pages WHERE run_id = $1", run_id) == "legacy"
            assert await conn.fetchval("SELECT to_regclass('public.crawl_jobs')") == "crawl_jobs"
        finally:
            await conn.close()
    finally:
        admin = await asyncpg.connect(admin_dsn)
        try:
            await admin.execute(f"DROP DATABASE IF EXISTS {quoted}")
        finally:
            await admin.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--admin-dsn", default=os.getenv("V040_MIGRATION_ADMIN_DSN"),
    )
    parser.add_argument("--database", default=TEST_DATABASE)
    args = parser.parse_args()
    if not args.admin_dsn:
        parser.error("--admin-dsn or V040_MIGRATION_ADMIN_DSN is required")
    return args


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(verify(args.admin_dsn, args.database))
