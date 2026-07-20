"""Persistence foundation tests.

Two guarantees:
  1. With DATABASE_URL unset every repo call is a silent no-op (legacy behaviour).
  2. With a database, the schema migrates and runs/pages/jobs round-trip — incl.
     the music-metadata join keys (content_hash column + metadata JSONB).
"""
import datetime

import pytest

from tests.conftest import requires_db


# --- 1. No-op safety (no database) -------------------------------------------

async def test_noop_without_db(monkeypatch):
    from app.db import pool, repo

    monkeypatch.delenv("DATABASE_URL", raising=False)
    await pool.reset_pool()

    assert pool.enabled() is False
    assert await pool.get_pool() is None

    # Writes return None and never raise.
    assert await repo.record_run_start() is None
    assert await repo.record_page(None, url="https://example.com") is None
    assert await repo.create_job(name="x", target_url="https://example.com") is None
    await repo.record_run_finish(None, status="completed")
    await repo.record_error(None, stage="x", message="y")
    await repo.mark_run_processing(None)

    # Reads return empty / None.
    assert await repo.list_jobs() == []
    assert await repo.claim_due_jobs() == []
    assert await repo.get_run(1) is None
    assert await repo.list_run_pages(1) == []


# --- 2. DB path: migrate -> insert -> read -----------------------------------

@requires_db
async def test_run_and_page_roundtrip(db):
    from app.db import repo

    run_id = await repo.record_run_start(
        external_id="20260615T000000__example__abc123",
        trigger="manual", status="processing")
    assert isinstance(run_id, int)

    page_id = await repo.record_page(
        run_id,
        url="https://example.com/song",
        engine="http", extractor="trafilatura",
        content_hash="deadbeef",
        extracted_text="# hello",
        raw_json_path="data/scrapes/x.json",
        metadata={"license": {"id": "CC-BY-4.0"},
                  "dedup": {"content_hash": "deadbeef"}},
    )
    assert isinstance(page_id, int)

    await repo.record_run_finish(run_id, status="completed",
                                 engine_used="http", pages_count=1,
                                 raw_output_path="data/scrapes/x.json")

    run = await repo.get_run(run_id)
    assert run["status"] == "completed"
    assert run["pages_count"] == 1
    assert run["external_id"].endswith("abc123")
    assert run["finished_at"] is not None

    pages = await repo.list_run_pages(run_id)
    assert len(pages) == 1
    assert pages[0]["content_hash"] == "deadbeef"          # promoted column
    assert pages[0]["metadata"]["license"]["id"] == "CC-BY-4.0"  # verbatim JSONB


@requires_db
async def test_content_hash_addressable_via_jsonb(db):
    """The music-metadata join key is reachable both ways (column + JSONB GIN)."""
    from app.db import repo, pool

    run_id = await repo.record_run_start(external_id="stem-jsonb", status="processing")
    await repo.record_page(
        run_id, url="https://e.com", content_hash="h1",
        metadata={"license": {"id": "MIT"}, "dedup": {"content_hash": "h1"}})

    p = await pool.get_pool()
    async with p.acquire() as conn:
        by_col = await conn.fetchval(
            "SELECT count(*) FROM scraped_pages WHERE content_hash = $1", "h1")
        by_json = await conn.fetchval(
            "SELECT count(*) FROM scraped_pages"
            " WHERE metadata @> '{\"dedup\":{\"content_hash\":\"h1\"}}'::jsonb")
    assert by_col == 1
    assert by_json == 1


@requires_db
async def test_extracted_record_roundtrip(db):
    """extracted_records FK to a page; list_records reaches them by run_id."""
    from app.db import repo

    run_id = await repo.record_run_start(external_id="rec-stem", status="processing")
    page_id = await repo.record_page(run_id, url="https://e.com/song")
    rid = await repo.record_extracted_record(
        page_id, source_url="https://e.com/song", record_type="extract",
        data_json={"artist": "X", "work": "Y"}, content_hash="rh1", confidence=0.5)
    assert isinstance(rid, int)

    recs = await repo.list_records(run_id=run_id)
    assert len(recs) == 1
    assert recs[0]["source_url"] == "https://e.com/song"
    assert recs[0]["record_type"] == "extract"
    assert recs[0]["data_json"]["artist"] == "X"      # JSONB decoded
    assert recs[0]["content_hash"] == "rh1"
    assert recs[0]["confidence"] == 0.5

    # record_type filter
    assert await repo.list_records(run_id=run_id, record_type="other") == []


@requires_db
async def test_migrations_idempotent(db):
    from app.db import migrate, pool

    # The fixture already migrated; a second run applies nothing new.
    assert await migrate.run_migrations() == 0
    p = await pool.get_pool()
    async with p.acquire() as conn:
        rows = await conn.fetchval("SELECT count(*) FROM schema_migrations")
    assert rows >= 1


# --- 3. Jobs + scheduler claim ------------------------------------------------

@requires_db
async def test_job_create_defaults_next_run(db):
    from app.db import repo

    job = await repo.create_job(
        name="hn", kind="scrape", target_url="https://news.ycombinator.com",
        params={"engine": "http"}, schedule="@hourly")
    assert job["id"]
    assert job["enabled"] is True
    assert job["next_run_at"] is not None          # scheduled => first run set
    fetched = await repo.get_job(job["id"])
    assert fetched["params"]["engine"] == "http"   # JSONB params decoded


@requires_db
async def test_claim_due_jobs_and_overlap_guard(db):
    from app.db import repo, pool

    job = await repo.create_job(
        name="due", kind="scrape", target_url="https://example.com",
        schedule="@hourly")
    p = await pool.get_pool()
    # Force it due.
    async with p.acquire() as conn:
        await conn.execute(
            "UPDATE scrape_jobs SET next_run_at = now() - interval '1 second'"
            " WHERE id = $1", job["id"])

    claimed = await repo.claim_due_jobs()
    assert any(c["id"] == job["id"] for c in claimed)

    # next_run_at advanced into the future, so an immediate re-claim is empty.
    assert await repo.claim_due_jobs() == []

    # Simulate an in-flight run, force due again -> overlap guard skips launch
    # (but still reschedules).
    await repo.record_run_start(job_id=job["id"], trigger="schedule", status="processing")
    async with p.acquire() as conn:
        await conn.execute(
            "UPDATE scrape_jobs SET next_run_at = now() - interval '1 second'"
            " WHERE id = $1", job["id"])
    claimed2 = await repo.claim_due_jobs()
    assert all(c["id"] != job["id"] for c in claimed2)


@requires_db
async def test_disabled_job_not_claimed(db):
    from app.db import repo, pool

    job = await repo.create_job(
        name="off", kind="scrape", target_url="https://example.com",
        schedule="@hourly", enabled=False)
    p = await pool.get_pool()
    async with p.acquire() as conn:
        await conn.execute(
            "UPDATE scrape_jobs SET next_run_at = now() - interval '1 second'"
            " WHERE id = $1", job["id"])
    claimed = await repo.claim_due_jobs()
    assert all(c["id"] != job["id"] for c in claimed)
