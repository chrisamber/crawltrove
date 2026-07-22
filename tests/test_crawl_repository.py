import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import requires_db


async def test_durable_repository_fails_closed_without_database(monkeypatch):
    from app.crawl import repository

    async def no_pool():
        return None

    monkeypatch.setattr(repository, "get_pool", no_pool)
    with pytest.raises(repository.PersistenceUnavailable):
        await repository.require_pool()


@requires_db
async def test_durable_crawl_schema_has_fencing_and_dedup_constraints(db):
    async with db.acquire() as conn:
        tables = await conn.fetchval("""
            SELECT count(*) FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name IN ('crawl_jobs','crawl_tasks','crawl_results',
                                 'crawl_origins','crawl_origin_leases',
                                 'crawl_events','acquisition_attempts')
        """)
        constraints = await conn.fetchval("""
            SELECT count(*) FROM pg_constraint
            WHERE conname IN ('crawl_tasks_job_url_unique',
                              'crawl_results_one_per_task')
        """)
    assert tables == 7
    assert constraints == 2


@requires_db
async def test_submit_is_idempotent_and_seeds_one_task(db):
    from app.crawl.config import CrawlConfig
    from app.crawl import repository

    config = CrawlConfig(url="https://example.com/docs")
    first = await repository.submit_job(config, idempotency_key="client-1")
    second = await repository.submit_job(config, idempotency_key="client-1")
    assert first == second
    async with db.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM crawl_tasks WHERE job_id = $1", first
        ) == 1


@requires_db
async def test_cancel_marks_unleased_tasks_terminal(db):
    from app.crawl.config import CrawlConfig
    from app.crawl import repository

    job_id = await repository.submit_job(CrawlConfig(url="https://example.com"))
    assert await repository.request_cancel(job_id) is True
    status = await repository.get_job(job_id)
    assert status["state"] == "cancelled"
    assert status["terminal_count"] == 1


@requires_db
async def test_two_workers_never_claim_the_same_task(db):
    from app.crawl import repository
    from app.crawl.config import CrawlConfig

    await repository.submit_job(CrawlConfig(url="https://example.com"))
    a, b = await asyncio.gather(
        repository.claim_task("worker-a", {"http"}),
        repository.claim_task("worker-b", {"http"}),
    )
    assert sum(task is not None for task in (a, b)) == 1


@requires_db
async def test_forced_browser_task_requires_browser_capability(db):
    from app.crawl import repository
    from app.crawl.config import CrawlConfig

    await repository.submit_job(CrawlConfig(
        url="https://example.com", engine="browser",
    ))
    assert await repository.claim_task("http-worker", {"http"}) is None
    assert await repository.claim_task("browser-worker", {"http", "browser"}) is not None


@requires_db
async def test_stale_lease_cannot_complete(db):
    from app.crawl import repository
    from app.crawl.config import CrawlConfig
    from app.crawl.types import TaskResult

    await repository.submit_job(CrawlConfig(url="https://example.com"))
    task = await repository.claim_task("worker-a", {"http"})
    assert task is not None
    assert await repository.complete_task(
        task.id, uuid.uuid4(), TaskResult(
            final_url=task.url, status_code=200, title="x", markdown="x"
        )
    ) is False


@requires_db
async def test_heartbeat_and_retry_release_reservations(db):
    from app.crawl import repository
    from app.crawl.classify import classify_failure
    from app.crawl.config import CrawlConfig

    job_id = await repository.submit_job(CrawlConfig(url="https://example.com"))
    task = await repository.claim_task("worker-a", {"http"})
    assert task is not None
    assert await repository.heartbeat(task.id, task.lease_token) is True
    assert await repository.retry_task(
        task.id, task.lease_token, classify_failure("timeout", None),
        datetime.now(timezone.utc) + timedelta(seconds=1),
    ) is True
    async with db.acquire() as conn:
        assert await conn.fetchval(
            "SELECT reserved_bytes FROM crawl_jobs WHERE id = $1", job_id
        ) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM crawl_origin_leases WHERE task_id = $1", task.id
        ) == 0
        assert await conn.fetchval(
            "SELECT outcome FROM acquisition_attempts WHERE task_id = $1", task.id
        ) == "retry"


@requires_db
async def test_reap_expired_lease_releases_reservations(db):
    from app.crawl import repository
    from app.crawl.config import CrawlConfig

    job_id = await repository.submit_job(CrawlConfig(url="https://example.com"))
    task = await repository.claim_task("worker-a", {"http"})
    assert task is not None
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE crawl_tasks SET lease_expires_at = now() - interval '1 second' WHERE id = $1",
            task.id,
        )
    assert await repository.reap_expired_leases() == 1
    async with db.acquire() as conn:
        assert await conn.fetchval(
            "SELECT state FROM crawl_tasks WHERE id = $1", task.id
        ) == "retry_wait"
        assert await conn.fetchval(
            "SELECT reserved_bytes FROM crawl_jobs WHERE id = $1", job_id
        ) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM crawl_origin_leases WHERE task_id = $1", task.id
        ) == 0
        assert await conn.fetchval(
            "SELECT outcome FROM acquisition_attempts WHERE task_id = $1", task.id
        ) == "retry"


@requires_db
async def test_completion_discovers_links_in_order_within_page_limit(db):
    from app.crawl import repository
    from app.crawl.config import CrawlConfig
    from app.crawl.types import TaskResult

    job_id = await repository.submit_job(CrawlConfig(
        url="https://example.com", limit=2, minDelayMs=0,
    ))
    task = await repository.claim_task("worker-a", {"http"})
    assert task is not None
    assert await repository.complete_task(task.id, task.lease_token, TaskResult(
        final_url=task.url,
        status_code=200,
        title="Example",
        markdown="ok",
        discovered_urls=(
            "https://example.com/next",
            "https://example.com/next#duplicate",
            "https://example.com/over-limit",
            "https://other.example/out-of-scope",
        ),
    ))
    async with db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT normalized_url, discovery_seq FROM crawl_tasks "
            "WHERE job_id = $1 ORDER BY discovery_seq",
            job_id,
        )
    assert [(row["normalized_url"], row["discovery_seq"]) for row in rows] == [
        ("https://example.com", 0),
        ("https://example.com/next", 1),
    ]
