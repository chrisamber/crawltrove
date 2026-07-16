"""SON-123: GET /api/runs — list runs, filter by jobId/status, paginate.

DB-path tests (skip cleanly without a local Postgres). Seed runs through the
repo, then exercise the list endpoint end-to-end through the real app.
"""
import httpx

from tests.conftest import requires_db


def _client():
    from app.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")


async def _seed_runs():
    """A job with two runs (completed + failed) plus one ad-hoc run (no job)."""
    from app.db import repo
    job = await repo.create_job(name="j", kind="scrape",
                                target_url="https://e.com", params={}, schedule=None)
    jid = job["id"]
    r1 = await repo.record_run_start(external_id="r1", job_id=jid,
                                     trigger="manual", status="completed")
    r2 = await repo.record_run_start(external_id="r2", job_id=jid,
                                     trigger="schedule", status="failed")
    r3 = await repo.record_run_start(external_id="r3", trigger="manual",
                                     status="completed")  # ad-hoc, job_id NULL
    return jid, r1, r2, r3


@requires_db
async def test_list_runs_returns_all_newest_first(db):
    _, r1, r2, r3 = await _seed_runs()
    async with _client() as c:
        resp = await c.get("/api/runs")
    assert resp.status_code == 200
    runs = resp.json()["runs"]
    assert [r["id"] for r in runs] == [r3, r2, r1]          # id DESC
    assert all("status" in r and "jobId" in r for r in runs)  # camelCase shape


@requires_db
async def test_list_runs_filters_by_job(db):
    jid, r1, r2, r3 = await _seed_runs()
    async with _client() as c:
        resp = await c.get("/api/runs", params={"jobId": jid})
    assert [r["id"] for r in resp.json()["runs"]] == [r2, r1]  # r3 ad-hoc, excluded


@requires_db
async def test_list_runs_filters_by_status(db):
    _, r1, r2, r3 = await _seed_runs()
    async with _client() as c:
        resp = await c.get("/api/runs", params={"status": "failed"})
    runs = resp.json()["runs"]
    assert [r["id"] for r in runs] == [r2]
    assert runs[0]["status"] == "failed"


@requires_db
async def test_list_runs_paginates(db):
    _, r1, r2, r3 = await _seed_runs()
    async with _client() as c:
        page1 = (await c.get("/api/runs", params={"limit": 2})).json()["runs"]
        page2 = (await c.get("/api/runs", params={"limit": 2, "offset": 2})).json()["runs"]
    assert [r["id"] for r in page1] == [r3, r2]
    assert [r["id"] for r in page2] == [r1]


async def test_list_runs_503_without_db(monkeypatch):
    from app.db import pool
    monkeypatch.delenv("DATABASE_URL", raising=False)
    await pool.reset_pool()
    async with _client() as c:
        assert (await c.get("/api/runs")).status_code == 503
