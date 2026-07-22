import asyncio
import uuid
from datetime import datetime, timezone

import pytest

from app.crawl.config import (
    AcquisitionConfig,
    BrightDataBudget,
    CreditBudgets,
    CrawlConfig,
    FirecrawlBudget,
)
from tests.conftest import requires_db


def test_provider_failure_retry_after_is_optional_and_preserves_existing_positionals():
    from app.acquisition.providers import NativeCost, ProviderFailure

    existing = ProviderFailure("provider_failure", True, NativeCost({}), 500)
    delayed = ProviderFailure(
        "provider_rate_limited", True, NativeCost({}), 429,
        retry_after_seconds=30,
    )
    assert existing.retry_after_seconds is None
    assert delayed.retry_after_seconds == 30


async def _claimed_provider_task(firecrawl_credits=2):
    from app.crawl import repository

    job_id = await repository.submit_job(CrawlConfig(
        url="https://example.com",
        minDelayMs=0,
        acquisition=AcquisitionConfig(creditBudgets=CreditBudgets(
            firecrawl=FirecrawlBudget(credits=firecrawl_credits),
            brightdata=BrightDataBudget(requests=3),
        )),
    ))
    task = await repository.claim_task("provider-test", {"http"})
    assert task is not None
    return job_id, task


@requires_db
async def test_provider_reservation_is_atomic(db):
    from app.crawl import repository

    _, task = await _claimed_provider_task(firecrawl_credits=1)
    first, second = await asyncio.gather(
        repository.reserve_acquisition_attempt(
            task.id, task.lease_token, "firecrawl_scrape", {"credits": 1},
        ),
        repository.reserve_acquisition_attempt(
            task.id, task.lease_token, "firecrawl_scrape", {"credits": 1},
        ),
    )
    assert sum(value is not None for value in (first, second)) == 1


@requires_db
async def test_route_attempt_cap_is_two(db):
    from app.crawl import repository

    _, task = await _claimed_provider_task()
    for _ in range(2):
        attempt = await repository.reserve_acquisition_attempt(
            task.id, task.lease_token, "brightdata_unlocker", {"requests": 1},
        )
        assert attempt is not None
        assert await repository.finish_acquisition_attempt(
            attempt.id, task.lease_token, "retryable_failure", {"requests": 1},
        ) is True
    assert await repository.reserve_acquisition_attempt(
        task.id, task.lease_token, "brightdata_unlocker", {"requests": 1},
    ) is None


@requires_db
async def test_total_provider_attempt_cap_is_four(db):
    from app.crawl import repository

    _, task = await _claimed_provider_task()
    for route, cost in (
        ("brightdata_unlocker", {"requests": 1}),
        ("brightdata_unlocker", {"requests": 1}),
        ("firecrawl_scrape", {"credits": 1}),
        ("firecrawl_interact", {"credits": 1}),
    ):
        attempt = await repository.reserve_acquisition_attempt(
            task.id, task.lease_token, route, cost,
        )
        assert attempt is not None
    assert await repository.reserve_acquisition_attempt(
        task.id, task.lease_token, "local_http", {},
    ) is None


@requires_db
async def test_provider_finish_is_fenced_and_reconciles_exact_meters(db):
    from app.acquisition.providers import ProviderProtocolError
    from app.crawl import repository

    job_id, task = await _claimed_provider_task()
    attempt = await repository.reserve_acquisition_attempt(
        task.id, task.lease_token, "firecrawl_scrape", {"credits": 2},
    )
    assert attempt is not None
    assert await repository.finish_acquisition_attempt(
        attempt.id, uuid.uuid4(), "succeeded", {"credits": 1},
    ) is False
    with pytest.raises(ProviderProtocolError):
        await repository.finish_acquisition_attempt(
            attempt.id, task.lease_token, "succeeded", {"credits": 3},
        )
    assert await repository.finish_acquisition_attempt(
        attempt.id, task.lease_token, "succeeded", {"credits": 1},
    ) is True
    async with db.acquire() as conn:
        usage = await conn.fetchrow(
            "SELECT reserved_value, consumed_value FROM crawl_provider_usage "
            "WHERE job_id = $1 AND provider = 'firecrawl' AND meter = 'credits'",
            job_id,
        )
        attempts = await conn.fetchval(
            "SELECT count(*) FROM acquisition_attempts WHERE task_id = $1", task.id,
        )
    assert (usage["reserved_value"], usage["consumed_value"]) == (0, 1)
    assert attempts == 2


@requires_db
async def test_provider_subattempts_preserve_worker_attempt_numbers(db):
    from app.crawl import repository
    from app.crawl.classify import classify_failure

    _, task = await _claimed_provider_task()
    provider_attempt = await repository.reserve_acquisition_attempt(
        task.id, task.lease_token, "firecrawl_scrape", {"credits": 1},
    )
    assert provider_attempt is not None
    assert await repository.finish_acquisition_attempt(
        provider_attempt.id, task.lease_token, "retryable_failure", {"credits": 1},
    )
    assert await repository.retry_task(
        task.id, task.lease_token, classify_failure("timeout", None),
        datetime.now(timezone.utc),
    )
    retried = await repository.claim_task("provider-test", {"http"})
    assert retried is not None
    async with db.acquire() as conn:
        numbers = await conn.fetch(
            "SELECT attempt_number FROM acquisition_attempts "
            "WHERE task_id = $1 ORDER BY started_at, attempt_number", task.id,
        )
    assert [row["attempt_number"] for row in numbers] == [1, 1001, 2]
