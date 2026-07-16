"""ResearchManager loop tests — search, scraper, LLM steps and storage are all
mocked; these exercise control flow, budgets, and terminal states."""
import pytest

from app import research_llm, search, storage
from app.research import ResearchManager


def _page(url):
    return {"success": True, "url": url, "title": f"T {url}",
            "description": "", "markdown": "body", "html": "<p>x</p>",
            "metadata": {"quality": {"score": 0.9}}}


@pytest.fixture
def manager(monkeypatch):
    m = ResearchManager()
    counter = {"n": 0}

    async def fake_scrape(url, **kw):
        return _page(url)

    async def fake_search(query, n=8):
        counter["n"] += 1
        return [{"url": f"https://ex.com/p{counter['n']}",
                 "title": f"t{counter['n']}", "snippet": "s"}]

    async def fake_plan(question, max_queries=4):
        return ["query one"]

    async def fake_select(question, candidates, k):
        return [c["url"] for c in candidates][:k]

    async def fake_notes(question, markdown, url):
        return {"relevant": True, "notes": "useful", "key_facts": ["fact"]}

    async def fake_assess(question, sources, rounds_left):
        return {"enough": True, "reasoning": "covered", "new_queries": []}

    async def fake_synth(question, sources):
        return {"report_markdown": "Answer [1].", "insufficient": False}

    monkeypatch.setattr(m.scraper, "scrape", fake_scrape)
    monkeypatch.setattr(search, "search", fake_search)
    monkeypatch.setattr(research_llm, "plan_queries", fake_plan)
    monkeypatch.setattr(research_llm, "select_urls", fake_select)
    monkeypatch.setattr(research_llm, "take_notes", fake_notes)
    monkeypatch.setattr(research_llm, "assess", fake_assess)
    monkeypatch.setattr(research_llm, "synthesize", fake_synth)
    monkeypatch.setattr(storage, "save_scrape", lambda result: "pagestem")
    monkeypatch.setattr(storage, "save_research", lambda job: "researchstem")
    monkeypatch.setattr(storage, "save_research_checkpoint", lambda job_id, payload: None)
    monkeypatch.setattr(storage, "delete_research_checkpoint", lambda job_id: None)
    return m


async def test_happy_path_completes(manager):
    jid = manager.create_job("what is x?")
    await manager.run_research(jid)
    job = manager.get_job(jid)
    assert job["status"] == "completed"
    assert job["rounds_run"] == 1
    assert job["pages_scraped"] == 1
    assert job["sources"][0]["artifact"] == "data/scrapes/pagestem.json"
    assert "Answer [1]." in job["report"]
    assert "## Sources" in job["report"]
    assert job["unverified_citations"] == []
    assert job["artifact_stem"] == "researchstem"
    assert job["end_time"] is not None


async def test_max_pages_budget_forces_synthesis(manager, monkeypatch):
    async def hungry_assess(question, sources, rounds_left):
        return {"enough": False, "reasoning": "more", "new_queries": ["another"]}

    monkeypatch.setattr(research_llm, "assess", hungry_assess)
    jid = manager.create_job("q", max_pages=2, max_rounds=10)
    await manager.run_research(jid)
    job = manager.get_job(jid)
    assert job["status"] == "completed"
    assert job["pages_scraped"] == 2
    assert job["report"] is not None


async def test_cancel_yields_partial(manager):
    jid = manager.create_job("q")
    manager.get_job(jid)["cancel_requested"] = True
    await manager.run_research(jid)
    job = manager.get_job(jid)
    assert job["status"] == "cancelled"
    assert job["report"] is not None


async def test_planner_failure_is_failed_with_partial(manager, monkeypatch):
    async def boom(question, max_queries=4):
        raise RuntimeError("backend down")

    monkeypatch.setattr(research_llm, "plan_queries", boom)
    jid = manager.create_job("q")
    await manager.run_research(jid)
    job = manager.get_job(jid)
    assert job["status"] == "failed"
    assert "backend down" in job["error"]
    assert job["report"] is not None


async def test_no_relevant_sources_is_honest(manager, monkeypatch):
    async def irrelevant(question, markdown, url):
        return {"relevant": False, "notes": "", "key_facts": []}

    monkeypatch.setattr(research_llm, "take_notes", irrelevant)
    jid = manager.create_job("q")
    await manager.run_research(jid)
    job = manager.get_job(jid)
    assert job["status"] == "completed"
    assert job["insufficient"] is True
    assert "Insufficient sources" in job["report"]


async def test_terminal_run_fires_research_webhook(manager, monkeypatch):
    from app import webhooks
    delivered = []

    async def fake_deliver(job):
        delivered.append(job["status"])
        return True

    monkeypatch.setattr(webhooks, "deliver_research", fake_deliver)
    jid = manager.create_job("what is x?")
    await manager.run_research(jid)
    assert delivered == ["completed"]


async def test_active_jobs_and_cancel_helpers(manager):
    jid = manager.create_job("q")
    assert manager.active_jobs() == [jid]
    assert manager.cancel(jid) is True
    manager.get_job(jid)["status"] = "completed"
    assert manager.cancel(jid) is False
    assert manager.active_jobs() == []
