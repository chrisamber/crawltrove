"""Tests for export, records listing, and Postgres full-text search.

DB-path tests (skip cleanly without a local Postgres). They seed pages directly
through the repo, then exercise the repo search + the export/search/records REST
endpoints end-to-end through the real app.
"""
import httpx

from tests.conftest import requires_db


def _client():
    from app.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")


async def _seed(external="run-exp"):
    from app.db import repo
    run_id = await repo.record_run_start(external_id=external, status="processing")
    p1 = await repo.record_page(
        run_id, url="https://e.com/python", status_code=200,
        engine="http", extractor="trafilatura", content_hash="c1",
        extracted_text="Python is a great programming language for data science",
        metadata={"title": "Python", "language": "en", "license": {"id": "MIT"}})
    p2 = await repo.record_page(
        run_id, url="https://e.com/rust", status_code=200,
        engine="http", extractor="trafilatura", content_hash="c2",
        extracted_text="Rust is a systems programming language with memory safety",
        metadata={"title": "Rust", "language": "en", "license": {"id": "Apache-2.0"}})
    await repo.record_run_finish(run_id, status="completed", pages_count=2)
    return run_id, p1, p2


# --- repo.search_pages (FTS) -------------------------------------------------

@requires_db
async def test_search_pages_matches_terms_and_excludes_others(db):
    from app.db import repo
    await _seed()
    hits = await repo.search_pages("python")
    assert [h["url"] for h in hits] == ["https://e.com/python"]
    assert hits[0]["rank"] is not None
    assert "Python" in (hits[0]["snippet"] or "")     # ts_headline snippet


@requires_db
async def test_search_pages_license_and_lang_filters(db):
    from app.db import repo
    await _seed()
    # "programming" appears in both; the license filter narrows to one.
    mit = await repo.search_pages("programming", license="MIT")
    assert len(mit) == 1
    assert mit[0]["url"].endswith("/python")
    # bogus language filter -> nothing
    assert await repo.search_pages("programming", lang="fr") == []


# --- export + records + search endpoints -------------------------------------

@requires_db
async def test_export_csv_streams_rows(db):
    run_id, _, _ = await _seed()
    async with _client() as c:
        resp = await c.get(f"/api/export.csv?runId={run_id}")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    lines = resp.text.splitlines()
    assert lines[0].startswith("id,url,status_code")     # header
    assert any("https://e.com/python" in ln for ln in lines)
    assert any("https://e.com/rust" in ln for ln in lines)


@requires_db
async def test_export_json_streams_array(db):
    run_id, _, _ = await _seed()
    async with _client() as c:
        resp = await c.get(f"/api/export.json?runId={run_id}")
    assert resp.status_code == 200
    arr = resp.json()
    assert {a["url"] for a in arr} == {"https://e.com/python", "https://e.com/rust"}
    assert all("contentHash" in a for a in arr)          # camelCase serialized


@requires_db
async def test_records_endpoint_lists_by_run(db):
    from app.db import repo
    run_id, p1, _ = await _seed()
    await repo.record_extracted_record(
        p1, source_url="https://e.com/python", record_type="extract",
        data_json={"lang": "python"}, content_hash="r1", confidence=0.8)
    async with _client() as c:
        resp = await c.get(f"/api/records?runId={run_id}")
    assert resp.status_code == 200
    records = resp.json()["records"]
    assert len(records) == 1
    assert records[0]["data"]["lang"] == "python"
    assert records[0]["sourceUrl"] == "https://e.com/python"


@requires_db
async def test_search_endpoint_returns_ranked_hits(db):
    await _seed()
    async with _client() as c:
        resp = await c.get("/api/search", params={"q": "memory safety", "lang": "en"})
    assert resp.status_code == 200
    hits = resp.json()["results"]
    assert any(h["url"].endswith("/rust") for h in hits)
    assert all("rank" in h for h in hits)


@requires_db
async def test_export_requires_a_selector(db):
    async with _client() as c:
        resp = await c.get("/api/export.csv")     # neither runId nor jobId
    assert resp.status_code == 400


async def test_export_search_503_without_db(monkeypatch):
    from app.db import pool
    monkeypatch.delenv("DATABASE_URL", raising=False)
    await pool.reset_pool()
    async with _client() as c:
        assert (await c.get("/api/export.csv?runId=1")).status_code == 503
        assert (await c.get("/api/export.json?runId=1")).status_code == 503
        assert (await c.get("/api/records?runId=1")).status_code == 503
        assert (await c.get("/api/search?q=x")).status_code == 503
