"""Batch scrape: job lifecycle, per-page pipeline, API wiring, DB indexing.

Hermetic like the other API tests: the singleton batcher's scraper is mocked,
storage goes to tmp dirs (or is mocked), and DB assertions run under
@requires_db against one run row per batch.
"""
import asyncio

import httpx
import pytest

from app import changes
from app.services import batcher
from tests.conftest import requires_db


def _canned(url, markdown="# page\n\nbody"):
    return {
        "success": True, "url": url, "title": "T", "description": "d",
        "markdown": markdown, "html": "<html/>",
        "metadata": {"url": url, "engine": "http", "status_code": 200},
    }


@pytest.fixture(autouse=True)
def _clean_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(changes, "INDEX_DIR", str(tmp_path))
    monkeypatch.setattr(changes, "HISTORY_PATH", str(tmp_path / "url_history.json"))
    monkeypatch.setattr(changes, "_history", None)
    batcher.jobs.clear()
    yield
    batcher.jobs.clear()


def _mock_pipeline(monkeypatch, fail_urls=()):
    from app import dedup, storage

    async def fake_scrape(url, **kw):
        if url in fail_urls:
            return {"success": False, "url": url, "error": "boom"}
        return _canned(url)

    monkeypatch.setattr(batcher, "scraper", type(
        "S", (), {"scrape": staticmethod(fake_scrape)})())
    monkeypatch.setattr(
        dedup, "check_and_register",
        lambda text, key: {"content_hash": "h-" + key[-1],
                           "exact_duplicate_of": None, "near_duplicate_of": None})
    monkeypatch.setattr(storage, "save_scrape", lambda result: "stem-x")
    monkeypatch.setattr(storage, "save_run_raw", lambda *a, **kw: {})


def _client():
    from app.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")


async def test_batch_runs_all_urls(monkeypatch):
    _mock_pipeline(monkeypatch)
    job_id = batcher.create_job(["https://x.test/a", "https://x.test/b"])
    await batcher.run_batch(job_id)

    job = batcher.get_job(job_id)
    assert job["status"] == "completed"
    assert job["progress"] == 1.0
    assert job["completed"] == 2 and job["total"] == 2
    assert {r["url"] for r in job["results"]} == {"https://x.test/a",
                                                  "https://x.test/b"}
    for r in job["results"]:
        assert r["metadata"]["dedup"]["content_hash"]
        assert r["metadata"]["changeTracking"]["changeStatus"] == "new"
        assert r["artifact"] == "data/scrapes/stem-x.json"
        assert "_raw" not in r


async def test_batch_partial_failure(monkeypatch):
    _mock_pipeline(monkeypatch, fail_urls={"https://x.test/bad"})
    job_id = batcher.create_job(["https://x.test/a", "https://x.test/bad"])
    await batcher.run_batch(job_id)

    job = batcher.get_job(job_id)
    assert job["status"] == "completed"          # one success is a completed batch
    assert len(job["results"]) == 1
    assert job["errors"] == [{"url": "https://x.test/bad", "error": "boom"}]


async def test_batch_all_failed(monkeypatch):
    _mock_pipeline(monkeypatch, fail_urls={"https://x.test/bad"})
    job_id = batcher.create_job(["https://x.test/bad"])
    await batcher.run_batch(job_id)
    assert batcher.get_job(job_id)["status"] == "failed"


async def test_batch_api_lifecycle(monkeypatch):
    _mock_pipeline(monkeypatch)
    async with _client() as c:
        resp = await c.post("/api/batch/scrape",
                            json={"urls": ["https://x.test/a"]})
        assert resp.status_code == 202
        job_id = resp.json()["jobId"]

        # BackgroundTasks runs after the response; poll until terminal.
        for _ in range(50):
            poll = await c.get(f"/api/batch/scrape/{job_id}")
            assert poll.status_code == 200
            if poll.json()["status"] in ("completed", "failed"):
                break
            await asyncio.sleep(0.01)
    body = poll.json()
    assert body["status"] == "completed"
    assert len(body["results"]) == 1


async def test_batch_api_validation():
    async with _client() as c:
        assert (await c.post("/api/batch/scrape",
                             json={"urls": []})).status_code == 422
        assert (await c.post("/api/batch/scrape",
                             json={"urls": ["ftp://x"]})).status_code == 400
        assert (await c.get("/api/batch/scrape/nope")).status_code == 404


@requires_db
async def test_batch_indexes_one_run_with_pages(db, monkeypatch):
    _mock_pipeline(monkeypatch)
    job_id = batcher.create_job(["https://x.test/a", "https://x.test/b"])
    await batcher.run_batch(job_id)

    async with db.acquire() as conn:
        runs = await conn.fetch("SELECT * FROM scrape_runs")
        pages = await conn.fetch("SELECT * FROM scraped_pages")
    assert len(runs) == 1
    assert runs[0]["external_id"] == job_id
    assert runs[0]["trigger"] == "batch"
    assert runs[0]["status"] == "completed"
    assert runs[0]["pages_count"] == 2
    assert len(pages) == 2
