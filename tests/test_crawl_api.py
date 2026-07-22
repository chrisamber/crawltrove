import asyncio

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
