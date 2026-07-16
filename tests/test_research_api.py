# tests/test_research_api.py
"""API wiring tests for /api/research — hermetic ASGI transport; the manager's
run loop is stubbed (loop behaviour is covered in tests/test_research.py)."""
import asyncio

import httpx
import pytest

from app import extract_llm
from app.services import researcher


def _client():
    from app.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")


@pytest.fixture(autouse=True)
def llm_configured(monkeypatch):
    monkeypatch.setattr(extract_llm, "configured", lambda: True)


@pytest.fixture(autouse=True)
def clean_jobs():
    researcher.jobs.clear()
    yield
    researcher.jobs.clear()


async def test_research_requires_llm_backend(monkeypatch):
    monkeypatch.setattr(extract_llm, "configured", lambda: False)
    async with _client() as c:
        resp = await c.post("/api/research", json={"query": "anything at all"})
    assert resp.status_code == 501


async def test_research_lifecycle(monkeypatch):
    async def fake_run(job_id):
        job = researcher.get_job(job_id)
        job["status"] = "completed"
        job["report"] = "# done"

    monkeypatch.setattr(researcher, "run_research", fake_run)
    async with _client() as c:
        resp = await c.post("/api/research", json={
            "query": "compare x and y", "maxRounds": 2, "maxPages": 5})
        assert resp.status_code == 202
        jid = resp.json()["jobId"]
        assert researcher.get_job(jid)["max_rounds"] == 2
        assert researcher.get_job(jid)["max_pages"] == 5

        body = None
        for _ in range(100):
            body = (await c.get(f"/api/research/{jid}")).json()
            if body["status"] == "completed":
                break
            await asyncio.sleep(0.02)
        assert body["status"] == "completed"
        assert body["report"] == "# done"


async def test_research_concurrency_cap(monkeypatch):
    async def never(job_id):
        pass

    monkeypatch.setattr(researcher, "run_research", never)
    async with _client() as c:
        assert (await c.post("/api/research",
                             json={"query": "run one"})).status_code == 202
        assert (await c.post("/api/research",
                             json={"query": "run two"})).status_code == 202
        third = await c.post("/api/research", json={"query": "run three"})
        assert third.status_code == 429
        assert len(third.json()["detail"]["activeJobs"]) == 2


async def test_cancel_and_missing_job():
    async with _client() as c:
        assert (await c.get("/api/research/nope")).status_code == 404
        assert (await c.post("/api/research/nope/cancel")).status_code == 404

        jid = researcher.create_job("manual job")
        resp = await c.post(f"/api/research/{jid}/cancel")
        assert resp.status_code == 200
        assert researcher.get_job(jid)["cancel_requested"] is True

        researcher.get_job(jid)["status"] = "completed"
        assert (await c.post(f"/api/research/{jid}/cancel")).status_code == 409


async def test_research_list_endpoint():
    async with _client() as c:
        assert (await c.get("/api/research")).json() == {"jobs": []}

        j1 = researcher.create_job("older query")
        researcher.get_job(j1)["start_time"] = "2026-07-01T00:00:00+00:00"
        researcher.get_job(j1)["status"] = "completed"
        researcher.get_job(j1)["report"] = "# secret body"
        j2 = researcher.create_job("newer query")
        researcher.get_job(j2)["start_time"] = "2026-07-09T00:00:00+00:00"

        body = (await c.get("/api/research")).json()
    jobs = body["jobs"]
    assert [j["job_id"] for j in jobs] == [j2, j1]      # newest first
    assert jobs[1]["query"] == "older query"
    assert jobs[1]["sources_count"] == 0
    # summaries only — no report/activity/sources bodies
    for j in jobs:
        assert "report" not in j and "activity" not in j and "sources" not in j


async def test_resume_endpoint(monkeypatch):
    resumed = []
    monkeypatch.setattr(researcher, "resume_research",
                        lambda job_id: resumed.append(job_id) or True)
    async with _client() as c:
        assert (await c.post("/api/research/nope/resume")).status_code == 404

        jid = researcher.create_job("interrupted job")
        # Only interrupted jobs can be resumed.
        assert (await c.post(f"/api/research/{jid}/resume")).status_code == 409

        researcher.get_job(jid)["status"] = "interrupted"
        resp = await c.post(f"/api/research/{jid}/resume")
        assert resp.status_code == 200
        assert resumed == [jid]


async def test_resume_endpoint_gates(monkeypatch):
    jid = researcher.create_job("interrupted job")
    researcher.get_job(jid)["status"] = "interrupted"

    async with _client() as c:
        # 501 without an LLM backend...
        monkeypatch.setattr(extract_llm, "configured", lambda: False)
        assert (await c.post(f"/api/research/{jid}/resume")).status_code == 501

        # ...and 429 at the concurrency cap (interrupted jobs don't count,
        # so fill the cap with two running ones).
        monkeypatch.setattr(extract_llm, "configured", lambda: True)
        for q in ("running one", "running two"):
            rid = researcher.create_job(q)
            researcher.get_job(rid)["status"] = "reading"
        assert (await c.post(f"/api/research/{jid}/resume")).status_code == 429
