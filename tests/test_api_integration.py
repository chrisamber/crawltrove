"""End-to-end wiring tests through the real FastAPI app (no network).

The scraper, dedup index, and file storage are mocked so these stay hermetic and
fast; what they exercise is the *wiring* added by Epic 1: the scrape path's DB
recording, the jobs/runs REST API, and the background job runner.
"""
import asyncio
import json

import httpx
import pytest

from tests.conftest import requires_db

CANNED_META = {
    "title": "Example", "description": "d", "url": "https://example.com",
    "engine": "http", "extractor": "trafilatura",
    "license": {"id": "MIT", "url": "", "source": "x", "evidence": ""},
    "quality": {"score": 1.0}, "language": "en",
}


def _canned(url):
    meta = dict(CANNED_META)
    meta["url"] = url
    return {
        "success": True, "url": url, "title": "Example", "description": "d",
        "markdown": "# Example\n\nbody text", "html": "<html>x</html>",
        "metadata": meta,
    }


def _mock_pipeline(monkeypatch):
    """Mock scraper.scrape + dedup + storage so no network/disk is touched."""
    import app.services as services
    from app import dedup, storage

    async def fake_scrape(url, **kw):
        return _canned(url)

    monkeypatch.setattr(services.scraper, "scrape", fake_scrape)
    monkeypatch.setattr(
        dedup, "check_and_register",
        lambda text, key: {"content_hash": "abc123",
                           "exact_duplicate_of": None, "near_duplicate_of": None})
    monkeypatch.setattr(storage, "save_scrape", lambda result: "teststem")


def _client():
    from app.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")


@requires_db
async def test_scrape_records_run_and_page(db, monkeypatch):
    _mock_pipeline(monkeypatch)
    async with _client() as c:
        resp = await c.post("/api/scrape", json={"url": "https://example.com"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["metadata"]["dedup"]["content_hash"] == "abc123"

    async with db.acquire() as conn:
        runs = await conn.fetch("SELECT * FROM scrape_runs")
        pages = await conn.fetch("SELECT * FROM scraped_pages")
    assert len(runs) == 1
    assert runs[0]["status"] == "completed"
    assert runs[0]["job_id"] is None              # ad-hoc
    assert runs[0]["external_id"] == "teststem"
    assert len(pages) == 1
    assert pages[0]["content_hash"] == "abc123"
    assert pages[0]["raw_json_path"] == "data/scrapes/teststem.json"
    # JSONB comes back as a string over a raw connection (no decode codec),
    # confirming it round-tripped as real JSONB.
    assert json.loads(pages[0]["metadata"])["license"]["id"] == "MIT"


@requires_db
async def test_jobs_run_runs_api(db, monkeypatch):
    _mock_pipeline(monkeypatch)
    async with _client() as c:
        created = await c.post("/api/jobs", json={
            "name": "t", "kind": "scrape",
            "targetUrl": "https://example.com", "params": {"engine": "http"}})
        assert created.status_code == 201
        job_id = created.json()["id"]
        assert created.json()["targetUrl"] == "https://example.com"

        run_resp = await c.post(f"/api/jobs/{job_id}/run")
        assert run_resp.status_code == 202
        run_id = run_resp.json()["runId"]

        # The runner executes as a background task; poll until it finishes.
        status = None
        for _ in range(60):
            got = await c.get(f"/api/runs/{run_id}")
            assert got.status_code == 200
            status = got.json()["status"]
            if status in ("completed", "failed"):
                break
            await asyncio.sleep(0.05)

        run = (await c.get(f"/api/runs/{run_id}")).json()
        assert run["status"] == "completed"
        assert run["jobId"] == job_id
        assert run["trigger"] == "manual"
        assert run["pagesCount"] == 1
        assert len(run["pages"]) == 1
        assert run["pages"][0]["contentHash"] == "abc123"

        listed = await c.get("/api/jobs")
        assert any(j["id"] == job_id for j in listed.json()["jobs"])

        got_job = await c.get(f"/api/jobs/{job_id}")
        assert got_job.status_code == 200
        assert got_job.json()["params"]["engine"] == "http"


@requires_db
async def test_scrape_persists_raw_capture(db, monkeypatch, tmp_path):
    """status_code + raw HTML + screenshot are captured to data/runs and indexed."""
    import app.services as services
    from app import dedup, storage

    async def fake_scrape(url, **kw):
        r = _canned(url)
        r["metadata"]["status_code"] = 200
        r["_raw"] = {"html": "<html>verbatim</html>", "screenshot": b"\x89PNG\r\n"}
        return r

    monkeypatch.setattr(services.scraper, "scrape", fake_scrape)
    monkeypatch.setattr(
        dedup, "check_and_register",
        lambda text, key: {"content_hash": "rawhash",
                           "exact_duplicate_of": None, "near_duplicate_of": None})
    monkeypatch.setattr(storage, "save_scrape", lambda result: "rawstem")
    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))

    async with _client() as c:
        resp = await c.post("/api/scrape", json={"url": "https://example.com"})
        assert resp.status_code == 200
        # The private raw channel must not leak into the API response.
        assert "_raw" not in resp.json()

    async with db.acquire() as conn:
        page = await conn.fetchrow("SELECT * FROM scraped_pages")
    assert page["status_code"] == 200
    assert page["raw_html_path"] == "data/runs/rawstem/page-1.html.txt"
    assert json.loads(page["metadata"])["screenshot_path"] == "data/runs/rawstem/page-1.png"
    assert (tmp_path / "runs" / "rawstem" / "page-1.html.txt").read_text() == "<html>verbatim</html>"
    assert (tmp_path / "runs" / "rawstem" / "page-1.png").read_bytes() == b"\x89PNG\r\n"


@requires_db
async def test_extract_persists_page_and_records(db, monkeypatch):
    """/api/extract records a run + page and one extracted_records row."""
    import app.services as services
    from app import dedup, storage, extract_llm

    async def fake_scrape(url, **kw):
        return _canned(url)

    async def fake_extract(markdown, url, schema, prompt="", model="", **kw):
        return {"data": {"artist": "X", "work": "Y"}, "model": "m",
                "usage": {"input_tokens": 1, "output_tokens": 2}}

    monkeypatch.setattr(services.scraper, "scrape", fake_scrape)
    monkeypatch.setattr(extract_llm, "configured", lambda: True)
    monkeypatch.setattr(extract_llm, "extract", fake_extract)
    monkeypatch.setattr(
        dedup, "check_and_register",
        lambda text, key: {"content_hash": "h-" + key[:8],
                           "exact_duplicate_of": None, "near_duplicate_of": None})
    monkeypatch.setattr(storage, "save_scrape", lambda result: "extractstem")

    async with _client() as c:
        resp = await c.post("/api/extract", json={
            "url": "https://example.com",
            "schema": {"type": "object",
                       "properties": {"artist": {"type": "string"}},
                       "required": ["artist"]}})
        assert resp.status_code == 200
        assert resp.json()["data"]["artist"] == "X"

    async with db.acquire() as conn:
        runs = await conn.fetch("SELECT * FROM scrape_runs")
        pages = await conn.fetch("SELECT * FROM scraped_pages")
        recs = await conn.fetch("SELECT * FROM extracted_records")
    assert len(runs) == 1 and runs[0]["status"] == "completed"
    assert len(pages) == 1
    assert len(recs) == 1
    assert recs[0]["source_url"] == "https://example.com"
    assert recs[0]["record_type"] == "extract"
    assert json.loads(recs[0]["data_json"])["artist"] == "X"
    assert recs[0]["content_hash"]                 # record-scoped hash set
    assert recs[0]["page_id"] == pages[0]["id"]


async def test_jobs_and_runs_503_without_db(monkeypatch):
    from app.db import pool
    monkeypatch.delenv("DATABASE_URL", raising=False)
    await pool.reset_pool()
    async with _client() as c:
        r1 = await c.post("/api/jobs", json={"targetUrl": "https://example.com"})
        assert r1.status_code == 503
        r2 = await c.get("/api/jobs")
        assert r2.status_code == 503
        r3 = await c.get("/api/runs/1")
        assert r3.status_code == 503


async def test_scrape_unaffected_without_db(monkeypatch):
    """Legacy path: scrape still returns 200 and the same body with no DB."""
    from app.db import pool
    monkeypatch.delenv("DATABASE_URL", raising=False)
    await pool.reset_pool()
    _mock_pipeline(monkeypatch)
    async with _client() as c:
        resp = await c.post("/api/scrape", json={"url": "https://example.com"})
        assert resp.status_code == 200
        assert resp.json()["metadata"]["dedup"]["content_hash"] == "abc123"
