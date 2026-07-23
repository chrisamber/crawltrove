import asyncio
import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.crawl.config import (
    AcquisitionConfig,
    BrightDataBudget,
    CreditBudgets,
    CrawlConfig,
    FirecrawlBudget,
)
from tests.conftest import requires_db


class _Adapter:
    def __init__(self, name, routes, outcomes, cost):
        self.name = name
        self.routes = frozenset(routes)
        self._outcomes = list(outcomes)
        self._cost = cost
        self.calls = []
        self.cancelled = []

    def available(self):
        return True

    def reserve_cost(self, request):
        return self._cost

    async def acquire(self, request):
        self.calls.append(request.route)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def cancel(self, remote_id):
        self.cancelled.append(remote_id)


class _Repository:
    def __init__(self):
        self.reserved = []
        self.finished = []
        self.browser_navigation_allowed = True

    async def reserve_browser_navigation(self, task_id, lease_token):
        return self.browser_navigation_allowed

    async def reserve_acquisition_attempt(self, task_id, lease_token, route, cost):
        self.reserved.append((task_id, lease_token, route, dict(cost)))
        return SimpleNamespace(id=uuid.uuid4())

    async def finish_acquisition_attempt(self, attempt_id, lease_token, outcome, cost, **kwargs):
        self.finished.append((attempt_id, lease_token, outcome, dict(cost), kwargs))
        return True


class _Scraper:
    def build_result(self, html, url, only_main_content, engine_used, status_code):
        return {"title": engine_used, "markdown": html, "metadata": {"engine": engine_used}}


def _router_task(*, engine="auto"):
    from app.crawl.types import ClaimedTask

    return ClaimedTask(
        id=uuid.uuid4(), job_id=uuid.uuid4(), url="https://example.com",
        normalized_url="https://example.com/", origin_key="https://example.com:443",
        depth=0, attempt=1, lease_token=uuid.uuid4(), deadline_at=datetime.now(timezone.utc),
        config={"engine": engine, "timeoutSeconds": 60, "onlyMainContent": True,
                "acquisition": {"provider": "auto"}},
        byte_allowance=1, artifact_allowance=1,
    )


def test_auto_route_map_is_exact():
    from app.acquisition.router import routes_for

    assert routes_for("ordinary") == ["local_http"]
    assert routes_for("static_block") == ["owned_proxy_http", "brightdata_unlocker"]
    assert routes_for("rendering") == ["local_browser", "firecrawl_scrape"]
    assert routes_for("interactive") == ["browserbase_session", "firecrawl_interact"]


async def test_router_falls_back_only_after_a_retryable_failure(monkeypatch):
    from app.acquisition.providers import NativeCost, ProviderFailure, ProviderResult
    from app.acquisition.registry import ProviderRegistry
    from app.acquisition.router import AcquisitionRouter

    async def public(_url):
        return None

    monkeypatch.setattr("app.acquisition.router.ensure_public_url", public)
    local = _Adapter(
        "local", {"local_browser"},
        [ProviderFailure("blocked_challenge", True, NativeCost({}))], NativeCost({}),
    )
    firecrawl = _Adapter(
        "firecrawl", {"firecrawl_scrape"},
        [ProviderResult("<p>ok</p>", "https://example.com", 200, NativeCost({"credits": 1}))],
        NativeCost({"credits": 1}),
    )
    repository = _Repository()
    result = await AcquisitionRouter(
        ProviderRegistry({"local": local, "firecrawl": firecrawl}), repository, _Scraper(),
    ).acquire(_router_task(engine="browser"))

    assert result.markdown == "<p>ok</p>"
    assert [row[2] for row in repository.reserved] == ["local_browser", "firecrawl_scrape"]
    assert [row[2] for row in repository.finished] == ["retryable_failure", "succeeded"]


async def test_router_reserves_browser_budget_before_local_navigation(monkeypatch):
    from app.acquisition.providers import NativeCost, ProviderFailure, ProviderResult
    from app.acquisition.registry import ProviderRegistry
    from app.acquisition.router import AcquisitionRouter

    async def public(_url):
        return None

    monkeypatch.setattr("app.acquisition.router.ensure_public_url", public)
    local = _Adapter(
        "local", {"local_browser"},
        [ProviderResult("<p>never</p>", "https://example.com", 200, NativeCost({}))],
        NativeCost({}),
    )
    repository = _Repository()
    repository.browser_navigation_allowed = False

    with pytest.raises(ProviderFailure, match="browser_budget_exhausted"):
        await AcquisitionRouter(
            ProviderRegistry({"local": local}), repository, _Scraper(),
        ).acquire(_router_task(engine="browser"))

    assert local.calls == []
    assert repository.reserved == []


async def test_local_challenge_transitions_once_to_static_block_routes(monkeypatch):
    from app.acquisition.providers import NativeCost, ProviderFailure, ProviderResult
    from app.acquisition.registry import ProviderRegistry
    from app.acquisition.router import AcquisitionRouter

    async def public(_url):
        return None

    monkeypatch.setattr("app.acquisition.router.ensure_public_url", public)
    local = _Adapter(
        "local", {"local_http", "owned_proxy_http"},
        [
            ProviderFailure("blocked_challenge", True, NativeCost({})),
            ProviderFailure("blocked_challenge", True, NativeCost({})),
        ], NativeCost({}),
    )
    brightdata = _Adapter(
        "brightdata", {"brightdata_unlocker"},
        [ProviderResult("<p>ok</p>", "https://example.com", 200, NativeCost({"requests": 1}))],
        NativeCost({"requests": 1}),
    )
    repository = _Repository()
    task = _router_task()
    task.config["acquisition"]["allowHumanIntervention"] = True
    result = await AcquisitionRouter(
        ProviderRegistry({"local": local, "brightdata": brightdata}), repository, _Scraper(),
    ).acquire(task)

    assert result.markdown == "<p>ok</p>"
    assert local.calls == ["local_http", "owned_proxy_http"]
    assert brightdata.calls == ["brightdata_unlocker"]
    assert [row[2] for row in repository.reserved] == [
        "local_http", "owned_proxy_http", "brightdata_unlocker",
    ]


async def test_normalize_preserves_downloaded_bytes_and_screenshot(monkeypatch):
    from app.acquisition.providers import NativeCost, ProviderResult
    from app.acquisition.registry import ProviderRegistry
    from app.acquisition.router import AcquisitionRouter

    async def public(_url):
        return None

    monkeypatch.setattr("app.acquisition.router.ensure_public_url", public)
    local = _Adapter(
        "local", {"local_http"},
        [ProviderResult(
            "<p>ok</p>", "https://example.com", 200, NativeCost({}),
            downloaded_bytes=4096, screenshot=b"PNG",
        )],
        NativeCost({}),
    )
    result = await AcquisitionRouter(
        ProviderRegistry({"local": local}), _Repository(), _Scraper(),
    ).acquire(_router_task())

    assert result.metadata["downloaded_bytes"] == 4096
    assert result.metadata["screenshot_bytes"] == 3


async def test_router_finishes_before_cancelling_ephemeral_remote_result(monkeypatch):
    from app.acquisition.providers import NativeCost, ProviderResult
    from app.acquisition.registry import ProviderRegistry
    from app.acquisition.router import AcquisitionRouter

    async def public(_url):
        return None

    monkeypatch.setattr("app.acquisition.router.ensure_public_url", public)
    local = _Adapter(
        "local", {"local_http"},
        [ProviderResult("<p>ok</p>", "https://example.com", 200, NativeCost({}), "remote-1")],
        NativeCost({}),
    )
    repository = _Repository()
    await AcquisitionRouter(
        ProviderRegistry({"local": local}), repository, _Scraper(),
    ).acquire(_router_task())

    assert repository.finished[0][2] == "succeeded"
    assert local.cancelled == ["remote-1"]


async def test_router_marks_protocol_error_unhealthy_and_finalizes(monkeypatch):
    from app.acquisition.providers import NativeCost, ProviderProtocolError
    from app.acquisition.registry import ProviderRegistry
    from app.acquisition.router import AcquisitionRouter

    async def public(_url):
        return None

    monkeypatch.setattr("app.acquisition.router.ensure_public_url", public)
    firecrawl = _Adapter(
        "firecrawl", {"firecrawl_scrape"},
        [ProviderProtocolError("malformed provider response")],
        NativeCost({"credits": 1}),
    )
    registry = ProviderRegistry({"firecrawl": firecrawl})
    repository = _Repository()
    task = _router_task(engine="browser")
    task.config["acquisition"]["provider"] = "firecrawl"

    with pytest.raises(Exception, match="provider_protocol_error"):
        await AcquisitionRouter(registry, repository, _Scraper()).acquire(task)

    assert repository.finished[0][2] == "failed"
    assert registry.health() == {"firecrawl": {"state": "unhealthy"}}


async def test_router_does_not_reclassify_programming_errors_as_provider_failure(monkeypatch):
    from app.acquisition.providers import NativeCost
    from app.acquisition.registry import ProviderRegistry
    from app.acquisition.router import AcquisitionRouter

    async def public(_url):
        return None

    monkeypatch.setattr("app.acquisition.router.ensure_public_url", public)
    local = _Adapter(
        "local", {"local_http"},
        [AttributeError("adapter bug")],
        NativeCost({}),
    )
    registry = ProviderRegistry({"local": local})
    repository = _Repository()

    with pytest.raises(AttributeError, match="adapter bug"):
        await AcquisitionRouter(registry, repository, _Scraper()).acquire(_router_task())

    assert repository.finished[0][2] == "failed"
    # Programming errors must not flip the adapter to unhealthy.
    assert registry.health()["local"]["state"] != "unhealthy"


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
async def test_abandoned_provider_reservation_is_conservatively_reconciled_on_cancel(db):
    from app.crawl import repository

    job_id, task = await _claimed_provider_task(firecrawl_credits=2)
    attempt = await repository.reserve_acquisition_attempt(
        task.id, task.lease_token, "firecrawl_scrape", {"credits": 2},
    )
    assert attempt is not None
    assert await repository.request_cancel(job_id)
    async with db.acquire() as conn:
        usage = await conn.fetchrow(
            "SELECT reserved_value, consumed_value FROM crawl_provider_usage "
            "WHERE job_id = $1 AND provider = 'firecrawl' AND meter = 'credits'", job_id,
        )
        abandoned = await conn.fetchrow(
            "SELECT outcome, error_code, actual_cost, cost_estimated "
            "FROM acquisition_attempts WHERE id = $1", attempt.id,
        )
    assert (usage["reserved_value"], usage["consumed_value"]) == (0, 2)
    actual = abandoned["actual_cost"]
    actual = json.loads(actual) if isinstance(actual, str) else dict(actual)
    assert actual == {"credits": 2}
    assert abandoned["outcome"] == "abandoned"
    assert abandoned["error_code"] == "lease_inactive"
    assert abandoned["cost_estimated"] is True


@requires_db
async def test_abandoned_provider_reservation_is_reconciled_on_lease_expiry(db):
    from app.crawl import repository

    job_id, task = await _claimed_provider_task(firecrawl_credits=1)
    attempt = await repository.reserve_acquisition_attempt(
        task.id, task.lease_token, "firecrawl_scrape", {"credits": 1},
    )
    assert attempt is not None
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE crawl_tasks SET lease_expires_at = now() - interval '1 second' WHERE id = $1",
            task.id,
        )
    assert await repository.reap_expired_leases() == 1
    async with db.acquire() as conn:
        usage = await conn.fetchrow(
            "SELECT reserved_value, consumed_value FROM crawl_provider_usage "
            "WHERE job_id = $1 AND provider = 'firecrawl' AND meter = 'credits'", job_id,
        )
        outcome = await conn.fetchval("SELECT outcome FROM acquisition_attempts WHERE id = $1", attempt.id)
    assert (usage["reserved_value"], usage["consumed_value"]) == (0, 1)
    assert outcome == "abandoned"


@requires_db
async def test_remote_provider_rpc_rejects_missing_extra_and_invalid_native_meters(db):
    import asyncpg
    from app.crawl import repository

    job_id = await repository.submit_job(CrawlConfig(
        url="https://example.com", minDelayMs=0,
        acquisition=AcquisitionConfig(
            provider="firecrawl",
            creditBudgets=CreditBudgets(firecrawl=FirecrawlBudget(credits=2)),
        ),
    ))
    worker_id = "provider-meter-test"
    async with db.acquire() as conn:
        role = await conn.fetchval("SELECT session_user")
        assert await conn.fetchval("SELECT id FROM workers WHERE db_role = $1", role) is None
        await conn.execute(
            """INSERT INTO workers (id, db_role, capabilities, protocol_version, state,
                                      artifact_bucket, artifact_prefix)
               VALUES ($1, $2, ARRAY['firecrawl_scrape'], 1, 'active', 'bucket', $3)""",
            worker_id, role, f"workers/{worker_id}/",
        )
        try:
            claim = await conn.fetchrow(
                "SELECT * FROM worker_api.claim(ARRAY['firecrawl_scrape']::TEXT[])"
            )
            assert claim is not None and claim["job_id"] == job_id
            for cost in ({}, {"credits": 1, "unexpected": 1}, {"credits": -1}):
                with pytest.raises(asyncpg.PostgresError, match="invalid provider native cost"):
                    await conn.fetchval(
                        "SELECT worker_api.reserve_provider_attempt($1,$2,'firecrawl_scrape',$3::jsonb)",
                        claim["id"], claim["lease_token"], json.dumps(cost),
                    )
            attempt_id = await conn.fetchval(
                "SELECT worker_api.reserve_provider_attempt($1,$2,'firecrawl_scrape',$3::jsonb)",
                claim["id"], claim["lease_token"], json.dumps({"credits": 1}),
            )
            assert attempt_id is not None
            for actual in ({}, {"credits": 1, "unexpected": 1}, {"credits": -1}):
                with pytest.raises(asyncpg.PostgresError):
                    await conn.fetchval(
                        "SELECT worker_api.finish_provider_attempt($1,$2,'succeeded',$3::jsonb,FALSE)",
                        attempt_id, claim["lease_token"], json.dumps(actual),
                    )
            assert await conn.fetchval(
                "SELECT worker_api.finish_provider_attempt($1,$2,'succeeded',$3::jsonb,FALSE)",
                attempt_id, claim["lease_token"], json.dumps({"credits": 1}),
            ) is True
        finally:
            await conn.execute("DELETE FROM workers WHERE id = $1", worker_id)


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


def test_env_registry_uses_release_brightdata_zone_name(monkeypatch):
    from app.acquisition.registry import env_registry

    monkeypatch.setenv("BRIGHTDATA_API_KEY", "test-key")
    monkeypatch.setenv("BRIGHTDATA_UNLOCKER_ZONE", "test-zone")
    monkeypatch.delenv("BRIGHTDATA_ZONE", raising=False)
    registry = env_registry(_Scraper())
    assert registry.health()["brightdata"] == {"state": "configured"}
