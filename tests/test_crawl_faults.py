import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from app.crawl.classify import classify_failure
from app.crawl.config import CrawlConfig
from app.crawl.types import TaskResult
from tests.conftest import requires_db


@requires_db
async def test_ten_workers_claim_unique_tasks(db):
    from app.crawl import repository

    for index in range(10):
        await repository.submit_job(CrawlConfig(url=f"https://example{index}.com"))
    claims = await asyncio.gather(*[
        repository.claim_task(f"worker-{index}", {"http"}) for index in range(10)
    ])
    ids = [claim.id for claim in claims if claim is not None]
    assert len(ids) == len(set(ids)) == 10


@requires_db
async def test_expired_lease_returns_to_retry_wait(db):
    from app.crawl import repository

    await repository.submit_job(CrawlConfig(url="https://example.com"))
    claim = await repository.claim_task("worker", {"http"})
    assert claim is not None
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE crawl_tasks SET lease_expires_at = now() - interval '1 second' WHERE id = $1",
            claim.id,
        )
    assert await repository.reap_expired_leases() == 1
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT state FROM crawl_tasks WHERE id = $1", claim.id) == "retry_wait"


@requires_db
async def test_cancel_and_deadline_reject_completion(db):
    from app.crawl import repository

    cancelled_job = await repository.submit_job(CrawlConfig(url="https://example.com"))
    cancelled = await repository.claim_task("worker", {"http"})
    assert cancelled is not None
    assert await repository.request_cancel(cancelled_job)
    assert not await repository.complete_task(
        cancelled.id, cancelled.lease_token,
        TaskResult(cancelled.url, 200, "x", "x"),
    )

    deadline_job = await repository.submit_job(CrawlConfig(url="https://example.net"))
    deadline = await repository.claim_task("worker", {"http"})
    assert deadline is not None
    async with db.acquire() as conn:
        await conn.execute("UPDATE crawl_jobs SET deadline_at = now() - interval '1 second' WHERE id = $1", deadline_job)
    assert not await repository.complete_task(
        deadline.id, deadline.lease_token,
        TaskResult(deadline.url, 200, "x", "x"),
    )


@requires_db
async def test_stale_completion_inserts_no_result(db):
    from app.crawl import repository

    await repository.submit_job(CrawlConfig(url="https://example.org"))
    claim = await repository.claim_task("worker", {"http"})
    assert claim is not None
    assert not await repository.complete_task(
        claim.id, uuid.uuid4(), TaskResult(claim.url, 200, "x", "x"),
    )
    async with db.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM crawl_results WHERE task_id = $1", claim.id
        ) == 0


@requires_db
async def test_two_jobs_share_one_origin_permit(db):
    from app.crawl import repository

    await repository.submit_job(CrawlConfig(url="https://example.com/a"))
    await repository.submit_job(CrawlConfig(url="https://example.com/b"))
    first, second = await asyncio.gather(
        repository.claim_task("worker-a", {"http"}),
        repository.claim_task("worker-b", {"http"}),
    )
    assert sum(claim is not None for claim in (first, second)) == 1


@requires_db
async def test_origin_delay_defers_a_second_job(db):
    from app.crawl import repository

    await repository.submit_job(CrawlConfig(url="https://example.com/a", minDelayMs=60000))
    first = await repository.claim_task("worker-a", {"http"})
    assert first is not None
    assert await repository.retry_task(
        first.id, first.lease_token, classify_failure("timeout", None),
        datetime.now(timezone.utc) + timedelta(minutes=1),
    )
    second_job = await repository.submit_job(CrawlConfig(url="https://example.com/b"))
    assert await repository.claim_task("worker-b", {"http"}) is None
    async with db.acquire() as conn:
        assert await conn.fetchval(
            "SELECT available_at > now() FROM crawl_tasks WHERE job_id = $1", second_job
        )


@requires_db
async def test_byte_reservations_never_exceed_the_job_budget(db):
    from app.crawl import repository

    job_id = await repository.submit_job(CrawlConfig(url="https://example.com", maxBytes=1))
    claim = await repository.claim_task("worker", {"http"})
    assert claim is not None and claim.byte_allowance == 1
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT reserved_bytes <= max_bytes FROM crawl_jobs WHERE id = $1", job_id)


@requires_db
async def test_attempts_record_outcome_and_failed_bytes(db):
    from app.crawl import repository

    await repository.submit_job(CrawlConfig(url="https://example.com"))
    claim = await repository.claim_task("worker", {"http"})
    assert claim is not None
    assert await repository.retry_task(
        claim.id, claim.lease_token, classify_failure("timeout", None),
        datetime.now(timezone.utc), {"engine": "http", "downloaded_bytes": 9},
    )
    async with db.acquire() as conn:
        attempt = await conn.fetchrow(
            "SELECT route, outcome, actual_cost FROM acquisition_attempts WHERE task_id = $1",
            claim.id,
        )
        assert attempt["route"] == "http"
        assert attempt["outcome"] == "retry"
        assert '"downloaded_bytes": 9' in attempt["actual_cost"]


@requires_db
async def test_artifact_overage_never_persists_markdown(db):
    from app.crawl import repository

    await repository.submit_job(CrawlConfig(url="https://example.com"))
    claim = await repository.claim_task("worker", {"http"})
    assert claim is not None
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE crawl_tasks SET artifact_budget_reserved = 1 WHERE id = $1", claim.id
        )
        await conn.execute(
            "UPDATE crawl_jobs SET reserved_artifact_bytes = 1 WHERE id = $1", claim.job_id
        )
    assert not await repository.complete_task(
        claim.id, claim.lease_token, TaskResult(claim.url, 200, "x", "too large"),
    )
    async with db.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM crawl_results WHERE task_id = $1", claim.id
        ) == 0


@requires_db
async def test_budget_exhaustion_drains_remaining_work(db):
    from app.crawl import repository

    job_id = await repository.submit_job(CrawlConfig(
        url="https://example.com", limit=2, minDelayMs=0, maxBytes=1,
    ))
    first = await repository.claim_task("worker", {"http"})
    assert first is not None
    assert await repository.complete_task(first.id, first.lease_token, TaskResult(
        first.url, 200, "first", "x", metadata={"downloaded_bytes": 1},
        discovered_urls=("https://example.com/second",),
    ))
    assert await repository.claim_task("worker", {"http"}) is None
    assert await repository.finalize_jobs() == 1
    status = await repository.get_job(job_id)
    assert status["state"] == "partial"
    assert status["terminal_reason"] == "byte_budget_exhausted"


@requires_db
async def test_max_failures_drains_remaining_work(db):
    from app.crawl import repository

    job_id = await repository.submit_job(CrawlConfig(
        url="https://example.com", limit=3, minDelayMs=0, maxFailures=1,
    ))
    first = await repository.claim_task("worker", {"http"})
    assert first is not None
    assert await repository.complete_task(first.id, first.lease_token, TaskResult(
        first.url, 200, "first", "ok",
        discovered_urls=("https://example.com/second", "https://example.com/third"),
    ))
    second = await repository.claim_task("worker", {"http"})
    assert second is not None
    assert await repository.fail_task(second.id, second.lease_token, classify_failure("unsafe_url", None))
    assert await repository.finalize_jobs() == 1
    status = await repository.get_job(job_id)
    assert status["state"] == "partial"
    assert status["terminal_reason"] == "max_failures_exceeded"


@requires_db
async def test_partial_and_reconciliation_match_task_rows(db):
    from app.crawl import repository

    job_id = await repository.submit_job(CrawlConfig(url="https://example.com", limit=2, minDelayMs=0))
    first = await repository.claim_task("worker", {"http"})
    assert first is not None
    assert await repository.complete_task(first.id, first.lease_token, TaskResult(
        first.url, 200, "first", "ok", discovered_urls=("https://example.com/second",),
    ))
    second = await repository.claim_task("worker", {"http"})
    assert second is not None
    assert await repository.fail_task(second.id, second.lease_token, classify_failure("unsafe_url", None))
    assert await repository.reconcile_job(job_id)
    assert await repository.finalize_jobs() == 1
    status = await repository.get_job(job_id)
    assert status["state"] == "partial"
    assert status["terminal_count"] == 2
