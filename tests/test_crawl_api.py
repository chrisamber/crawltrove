import asyncio
from datetime import datetime, timezone
from uuid import UUID

import httpx

from tests.conftest import requires_db


async def test_crawl_requires_database_but_scrape_does_not(monkeypatch):
    from app.crawl.repository import PersistenceUnavailable
    from app.routes import routes_crawl
    from app import main
    from app.main import app

    async def unavailable(config, **kwargs):
        raise PersistenceUnavailable("database unavailable")

    async def scrape(url, **kwargs):
        return {
            "success": True, "url": url, "title": "Example", "markdown": "ok",
            "html": "<p>ok</p>", "metadata": {"engine": "http"}, "_raw": {},
        }

    async def persisted(result, raw, **kwargs):
        return {"run_id": None, "page_id": None, "stem": None}

    monkeypatch.setattr(routes_crawl.crawl_service, "submit_crawl", unavailable)
    monkeypatch.setattr(main.scraper, "scrape", scrape)
    monkeypatch.setattr(main.runner, "persist_scrape_page", persisted)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        crawl = await client.post("/api/crawl", json={"url": "https://example.com"})
        scrape_response = await client.post("/api/scrape", json={"url": "https://example.com"})
    assert crawl.status_code == 503
    assert crawl.json()["detail"]["code"] == "persistence_unavailable"
    assert scrape_response.status_code == 200


async def test_crawl_service_polls_then_stops_cleanly():
    from app.crawl.service import CrawlService

    class Worker:
        def __init__(self):
            self.calls = 0

        async def run_once(self):
            self.calls += 1
            return False

    worker = Worker()
    service = CrawlService(worker=worker)
    await service.start()
    await asyncio.sleep(0)
    await service.stop()
    assert worker.calls >= 1


async def test_remote_worker_mode_does_not_run_local_acquisition(monkeypatch):
    from app.crawl.service import CrawlService

    class Worker:
        calls = 0

        async def run_once(self):
            self.calls += 1
            return False

    monkeypatch.setenv("CRAWLTROVE_REMOTE_WORKERS", "true")
    worker = Worker()
    service = CrawlService(worker=worker)
    await service.start()
    await asyncio.sleep(0)
    await service.stop()
    assert worker.calls == 0


async def test_pages_cursor_includes_seed_and_unknown_job_is_404(monkeypatch):
    from app.main import app
    from app.routes import routes_crawl

    async def pages(job_id, after, limit):
        if str(job_id).endswith("0001"):
            return [{
                "discovery_seq": 0,
                "state": "succeeded",
                "markdown": "# Seed",
            }]
        return None

    monkeypatch.setattr(routes_crawl.repository, "list_pages", pages)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        found = await client.get(
            "/api/crawl/00000000-0000-0000-0000-000000000001/pages"
        )
        missing = await client.get(
            "/api/crawl/00000000-0000-0000-0000-000000000002/pages"
        )
    assert found.status_code == 200
    assert found.json()["pages"][0]["markdown"] == "# Seed"
    assert found.json()["nextAfter"] == 0
    assert missing.status_code == 404


async def test_status_preserves_one_release_legacy_resume_bridge(monkeypatch):
    from app.main import app
    from app.routes import routes_crawl

    legacy_id = "00000000-0000-0000-0000-000000000003"

    async def missing_durable(job_id):
        return None

    monkeypatch.setattr(routes_crawl.repository, "get_job", missing_durable)
    monkeypatch.setattr(
        routes_crawl.crawler,
        "get_job",
        lambda job_id: {
            "id": job_id,
            "status": "processing",
            "progress": 0.5,
            "results": [],
            "errors": [],
        } if job_id == legacy_id else None,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(f"/api/crawl/{legacy_id}")

    assert response.status_code == 200
    assert response.json()["status"] == "processing"


@requires_db
async def test_explicit_provider_budget_is_rejected_before_job_insert(db, monkeypatch):
    from app.crawl import service
    from app.main import app

    monkeypatch.setattr(service.crawl_service.registry, "require_available", lambda _provider: None)
    async def public_url(_url):
        return ()
    monkeypatch.setattr(service, "ensure_public_url", public_url)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/api/crawl", json={
            "url": "https://example.com",
            "timeoutSeconds": 30,
            "acquisition": {
                "provider": "browserbase",
                "creditBudgets": {
                    "browserbase": {"browserMinutes": 1, "proxyBytes": 0},
                },
            },
        })

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "provider_budget_invalid"
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM crawl_jobs") == 0


@requires_db
async def test_status_exposes_only_safe_provider_attempt_and_usage_fields(db):
    from app.crawl import repository
    from app.crawl.config import (
        AcquisitionConfig, CreditBudgets, CrawlConfig, FirecrawlBudget,
    )
    from app.main import app

    job_id = await repository.submit_job(CrawlConfig(
        url="https://example.com", minDelayMs=0,
        acquisition=AcquisitionConfig(
            creditBudgets=CreditBudgets(firecrawl=FirecrawlBudget(credits=2)),
        ),
    ))
    task = await repository.claim_task("safe-api-worker", {"http"})
    assert task is not None
    attempt = await repository.reserve_acquisition_attempt(
        task.id, task.lease_token, "firecrawl_scrape", {"credits": 1},
    )
    assert attempt is not None
    assert await repository.finish_acquisition_attempt(
        attempt.id, task.lease_token, "succeeded", {"credits": 1},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(f"/api/crawl/{job_id}")

    assert response.status_code == 200
    body = response.json()
    managed = next(item for item in body["attempts"] if item["id"] == str(attempt.id))
    assert set(managed) == {
        "id", "route", "provider", "outcome", "blockReason", "errorCode",
        "durationMs", "nativeUsage", "startedAt", "finishedAt",
    }
    assert managed["nativeUsage"] == {"credits": 1}
    assert body["usage"] == [{
        "provider": "firecrawl", "meter": "credits", "limit": 2.0,
        "reserved": 0.0, "consumed": 1.0,
    }]
    serialized = response.text.lower()
    for forbidden in ("lease_token", "api_key", "remote_session", "connecturl", "wsurl"):
        assert forbidden not in serialized


async def test_scheduled_crawl_returns_compatibility_run_id(monkeypatch):
    from app import runner

    async def submit(config, **kwargs):
        return "00000000-0000-0000-0000-000000000001"

    async def get_job(job_id):
        return {"run_id": 42}

    monkeypatch.setattr(runner, "submit_crawl", submit)
    monkeypatch.setattr(runner.crawl_repository, "get_job", get_job)
    run_id = await runner.launch_job({
        "id": 7,
        "kind": "crawl",
        "target_url": "https://example.com",
        "params": {"limit": 2},
    })
    assert run_id == 42


async def test_events_resume_from_last_event_id_and_close_at_terminal(monkeypatch):
    from app.main import app
    from app.routes import routes_crawl

    seen = []

    async def list_events(_job_id, after, limit):
        seen.append((after, limit))
        if limit == 1:
            return []
        if after < 5:
            return [{
                "id": 5, "task_id": None, "event": "job_completed",
                "metadata": {}, "created_at": datetime.now(timezone.utc),
            }]
        return []

    async def job_state(_job_id):
        return "completed"

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(routes_crawl.repository, "list_events", list_events)
    monkeypatch.setattr(routes_crawl.repository, "job_state", job_state)
    monkeypatch.setattr(routes_crawl.asyncio, "sleep", no_sleep)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.get(
            "/api/crawl/00000000-0000-0000-0000-000000000001/events",
            headers={"Last-Event-ID": "4"},
        )
        invalid = await client.get(
            "/api/crawl/00000000-0000-0000-0000-000000000001/events",
            headers={"Last-Event-ID": str(2**63)},
        )

    assert response.status_code == 200
    assert "id: 5\nevent: job_completed" in response.text
    assert seen[0] == (4, 1) and (4, 100) in seen
    assert invalid.status_code == 400


@requires_db
async def test_retry_failures_creates_new_job_without_mutating_source(db):
    from app.crawl import repository
    from app.crawl.classify import FailureDecision
    from app.crawl.config import CrawlConfig
    from app.main import app

    source_id = await repository.submit_job(CrawlConfig(
        url="https://example.com/fail", minDelayMs=0,
    ))
    source_task = await repository.claim_task("retry-source", {"http"})
    assert source_task is not None
    assert await repository.fail_task(
        source_task.id, source_task.lease_token,
        FailureDecision(False, "transport", "fixture_failure"),
    )

    other_id = await repository.submit_job(CrawlConfig(
        url="https://example.org/foreign", minDelayMs=0,
    ))
    other_task = await repository.claim_task("retry-foreign", {"http"})
    assert other_task is not None and other_task.job_id == other_id
    assert await repository.fail_task(
        other_task.id, other_task.lease_token,
        FailureDecision(False, "transport", "fixture_failure"),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        retried = await client.post(f"/api/crawl/{source_id}/retry-failures")
        rejected = await client.post(
            f"/api/crawl/{source_id}/retry-failures",
            json={"taskIds": [str(other_task.id)]},
        )

    assert retried.status_code == 202
    retry_id = retried.json()["jobId"]
    assert retry_id != str(source_id)
    assert rejected.status_code == 409
    async with db.acquire() as conn:
        source_state = await conn.fetchval(
            "SELECT state FROM crawl_tasks WHERE id = $1", source_task.id,
        )
        retry_urls = await conn.fetch(
            "SELECT original_url FROM crawl_tasks WHERE job_id = $1", UUID(retry_id),
        )
    assert source_state == "permanent_failed"
    assert [row["original_url"] for row in retry_urls] == [source_task.url]


@requires_db
async def test_durable_submit_survives_pool_restart_and_exposes_page(db, monkeypatch):
    from app.crawl import repository, service
    from app.crawl.types import TaskResult
    from app.db import pool
    from app.main import app

    async def public_url(url):
        return ()

    monkeypatch.setattr(service, "ensure_public_url", public_url)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        submitted = await client.post("/api/crawl", json={
            "url": "https://example.com", "limit": 1, "minDelayMs": 0,
        })
        assert submitted.status_code == 202
        job_id = submitted.json()["jobId"]
        task = await repository.claim_task("test-worker", {"http"})
        assert task is not None
        assert await repository.complete_task(
            task.id,
            task.lease_token,
            TaskResult(
                final_url=task.url,
                status_code=200,
                title="Example",
                markdown="# Durable",
                metadata={"downloaded_bytes": 9},
            ),
        )
        assert await repository.finalize_jobs() == 1

        await pool.reset_pool()
        status_response = await client.get(f"/api/crawl/{job_id}")
        pages_response = await client.get(f"/api/crawl/{job_id}/pages")

    assert status_response.status_code == 200
    assert status_response.json()["status"] == "completed"
    assert status_response.json()["resultCount"] == 1
    assert pages_response.status_code == 200
    assert pages_response.json()["pages"][0]["markdown"] == "# Durable"
