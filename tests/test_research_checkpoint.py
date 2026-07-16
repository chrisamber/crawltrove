"""Deep-research Phase 2: checkpoint/resume.

All LLM/search/scrape/storage calls are mocked like tests/test_research.py;
checkpoints go to a tmp DATA_DIR. The restart simulation builds a FRESH
ResearchManager over the same checkpoint dir — nothing in-memory survives.
"""
import json

import pytest

from app import research_llm, search, storage
from app.research import MAX_CONCURRENT, ResearchManager
from tests.conftest import requires_db


def _page(url):
    return {"success": True, "url": url, "title": f"T {url}",
            "description": "", "markdown": "body", "html": "<p>x</p>",
            "metadata": {"quality": {"score": 0.9}}}


@pytest.fixture
def tmp_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(storage, "SCRAPES_DIR", str(tmp_path / "scrapes"))
    monkeypatch.setattr(storage, "CRAWLS_DIR", str(tmp_path / "crawls"))
    monkeypatch.setattr(storage, "RESEARCH_DIR", str(tmp_path / "research"))
    monkeypatch.setattr(storage, "RESEARCH_CHECKPOINTS_DIR",
                        str(tmp_path / "research" / "checkpoints"))
    return tmp_path


def _mock_llm(monkeypatch, scraped_urls):
    async def fake_search(query, n=8):
        return [{"url": f"https://ex.com/{query}-{i}", "title": "t", "snippet": "s"}
                for i in range(3)]

    async def fake_plan(question, max_queries=4):
        return ["q1"]

    async def fake_select(question, candidates, k):
        return [c["url"] for c in candidates][:k]

    async def fake_notes(question, markdown, url):
        return {"relevant": True, "notes": "n", "key_facts": ["f"]}

    async def fake_assess(question, sources, rounds_left):
        return {"enough": True, "reasoning": "done", "new_queries": []}

    async def fake_synth(question, sources):
        return {"report_markdown": "Answer [1].", "insufficient": False}

    monkeypatch.setattr(search, "search", fake_search)
    monkeypatch.setattr(research_llm, "plan_queries", fake_plan)
    monkeypatch.setattr(research_llm, "select_urls", fake_select)
    monkeypatch.setattr(research_llm, "take_notes", fake_notes)
    monkeypatch.setattr(research_llm, "assess", fake_assess)
    monkeypatch.setattr(research_llm, "synthesize", fake_synth)
    monkeypatch.setattr(storage, "save_scrape", lambda result: "pagestem")

    def scraper_for(m):
        async def fake_scrape(url, **kw):
            scraped_urls.append(url)
            return _page(url)
        m.scraper.scrape = fake_scrape

    return scraper_for


def _checkpoint_path(tmp_dirs, job_id):
    return tmp_dirs / "research" / "checkpoints" / f"{job_id}.json"


async def test_checkpoint_written_and_deleted_at_terminal(tmp_dirs, monkeypatch):
    scraped = []
    scraper_for = _mock_llm(monkeypatch, scraped)
    seen_payloads = []
    real_save = storage.save_research_checkpoint

    def spying_save(job_id, payload):
        seen_payloads.append(json.loads(json.dumps(payload)))
        real_save(job_id, payload)

    monkeypatch.setattr(storage, "save_research_checkpoint", spying_save)

    m = ResearchManager()
    scraper_for(m)
    jid = m.create_job("what is x?")
    await m.run_research(jid)

    assert m.get_job(jid)["status"] == "completed"
    # Checkpoints were written during the run (plan, per-page claim+result, ...)
    assert len(seen_payloads) >= 3
    assert any(p["loop"]["phase"] == "read" for p in seen_payloads)
    assert all(p["version"] == 1 for p in seen_payloads)
    # ...and removed once the artifact was saved.
    assert not _checkpoint_path(tmp_dirs, jid).exists()


async def test_restart_rehydrates_as_interrupted(tmp_dirs, monkeypatch):
    scraped = []
    scraper_for = _mock_llm(monkeypatch, scraped)

    # Crash the run mid-read: the second page read raises hard.
    m = ResearchManager()

    async def crashing_scrape(url, **kw):
        if len(scraped) >= 1:
            raise KeyboardInterrupt  # not caught by _read_page's except Exception
        scraped.append(url)
        return _page(url)

    m.scraper.scrape = crashing_scrape
    jid = m.create_job("q", max_pages=5)
    with pytest.raises(KeyboardInterrupt):
        await m.run_research(jid)
    assert _checkpoint_path(tmp_dirs, jid).exists()

    # "Restart": a brand-new manager over the same checkpoint dir.
    m2 = ResearchManager()
    restored = m2.restore_from_checkpoints()
    assert restored == [jid]
    job = m2.get_job(jid)
    assert job["status"] == "interrupted"
    assert job["pages_scraped"] == 1
    assert len(job["sources"]) == 1
    # Interrupted jobs don't count against the concurrency cap.
    assert m2.active_jobs() == []


async def test_resume_completes_without_rereading(tmp_dirs, monkeypatch):
    scraped = []
    scraper_for = _mock_llm(monkeypatch, scraped)

    m = ResearchManager()

    async def crashing_scrape(url, **kw):
        if len(scraped) >= 2:
            raise KeyboardInterrupt
        scraped.append(url)
        return _page(url)

    m.scraper.scrape = crashing_scrape
    jid = m.create_job("q", max_pages=10)
    with pytest.raises(KeyboardInterrupt):
        await m.run_research(jid)
    first_pass = list(scraped)
    assert len(first_pass) == 2

    m2 = ResearchManager()
    scraper_for(m2)
    assert m2.restore_from_checkpoints() == [jid]
    assert m2.resume_research(jid) is True
    # resume_research dispatches a task; await it directly for determinism.
    for t in list(m2._tasks):
        await t

    job = m2.get_job(jid)
    assert job["status"] == "completed"
    # No URL read before the crash was read again after the resume...
    resumed = scraped[len(first_pass):]
    assert not set(first_pass) & set(resumed)
    # ...the crashed-mid-read URL was claimed on disk before the read, so the
    # resume skips it entirely (no crash-loop, no double count)...
    crashed_url = "https://ex.com/q1-2"
    assert crashed_url not in resumed
    assert crashed_url not in [s["url"] for s in job["sources"]]
    assert job["pages_scraped"] == len(first_pass) + len(resumed)
    assert job["pages_scraped"] <= job["max_pages"]
    # ...and the sources kept the pre-crash reads.
    urls = [s["url"] for s in job["sources"]]
    assert set(first_pass) <= set(urls)
    assert not _checkpoint_path(tmp_dirs, jid).exists()


async def test_resume_past_deadline_synthesizes_only(tmp_dirs, monkeypatch):
    scraped = []
    scraper_for = _mock_llm(monkeypatch, scraped)

    m = ResearchManager()

    async def crashing_scrape(url, **kw):
        if scraped:
            raise KeyboardInterrupt
        scraped.append(url)
        return _page(url)

    m.scraper.scrape = crashing_scrape
    jid = m.create_job("q", max_pages=10)
    with pytest.raises(KeyboardInterrupt):
        await m.run_research(jid)

    m2 = ResearchManager()
    scraper_for(m2)
    m2.restore_from_checkpoints()
    # Force the wall-clock deadline into the past.
    m2.get_job(jid)["deadline_utc"] = "2020-01-01T00:00:00+00:00"
    m2.resume_research(jid)
    for t in list(m2._tasks):
        await t

    job = m2.get_job(jid)
    assert job["status"] == "completed"
    assert job["report"] is not None
    assert len(scraped) == 1          # nothing new was read past the deadline


async def test_terminal_checkpoint_leftover_is_cleaned(tmp_dirs, monkeypatch):
    """Crash between save_research and checkpoint delete: restore cleans it."""
    storage.save_research_checkpoint("dead-job", {
        "version": 1, "job": {"job_id": "dead-job", "status": "completed"},
        "loop": {}, "updated_at": "2026-01-01T00:00:00+00:00"})
    m = ResearchManager()
    assert m.restore_from_checkpoints() == []
    assert not _checkpoint_path(tmp_dirs, "dead-job").exists()


async def test_corrupt_checkpoint_is_skipped(tmp_dirs):
    ckdir = tmp_dirs / "research" / "checkpoints"
    ckdir.mkdir(parents=True, exist_ok=True)
    (ckdir / "bad.json").write_text("{not json")
    m = ResearchManager()
    assert m.restore_from_checkpoints() == []


async def test_resume_requires_interrupted_status(tmp_dirs, monkeypatch):
    m = ResearchManager()
    jid = m.create_job("q")
    assert m.resume_research(jid) is False        # queued, not interrupted
    assert m.resume_research("unknown") is False


@requires_db
async def test_research_run_indexed_in_db(db, tmp_dirs, monkeypatch):
    scraped = []
    scraper_for = _mock_llm(monkeypatch, scraped)
    m = ResearchManager()
    scraper_for(m)
    jid = m.create_job("indexed query")
    await m.run_research(jid)

    async with db.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM research_runs")
    assert len(rows) == 1
    assert rows[0]["job_id"] == jid
    assert rows[0]["query"] == "indexed query"
    assert rows[0]["status"] == "completed"
    assert rows[0]["finished_at"] is not None
