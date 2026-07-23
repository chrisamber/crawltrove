"""llms.txt generation: pure format, storage pair, and the 202+poll API flow."""
import asyncio
from uuid import uuid4

import httpx
import pytest

from app import llmstxt, storage


def _page(url, title, description="", markdown="# body"):
    return {"url": url, "title": title, "description": description,
            "markdown": markdown}


def test_generate_format():
    txt, full = llmstxt.generate("https://site.test", [
        _page("https://site.test/", "Site Home", "The landing page"),
        _page("https://site.test/docs", "Docs", markdown="# Docs\n\ncontent"),
    ])
    lines = txt.splitlines()
    assert lines[0] == "# Site Home"          # site title = first page title
    assert lines[1] == ""
    assert lines[2] == "- [Site Home](https://site.test/): The landing page"
    assert lines[3] == "- [Docs](https://site.test/docs)"   # no desc → no colon
    assert "# Docs\nhttps://site.test/docs\n\n# Docs\n\ncontent" in full
    assert "\n\n---\n\n" in full


def test_generate_empty_and_fallbacks():
    txt, full = llmstxt.generate("https://site.test/path", [])
    assert txt == "# site.test\n\n"           # host fallback title
    assert full == ""

    txt, _ = llmstxt.generate("https://site.test", [
        _page("https://site.test/x", "", "d\n  multi   line")])
    assert "- [https://site.test/x](https://site.test/x): d multi line" in txt


def test_pages_from_durable_job():
    pages = llmstxt.pages_from_durable_job({
        "results": [{
            "final_url": "https://s.test/",
            "title": "Home",
            "markdown": "# hi",
            "metadata": {"description": "front door"},
        }],
    })
    assert pages == [_page("https://s.test/", "Home", "front door", "# hi")]


def test_save_llmstxt_pair(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(storage, "SCRAPES_DIR", str(tmp_path / "scrapes"))
    monkeypatch.setattr(storage, "CRAWLS_DIR", str(tmp_path / "crawls"))
    monkeypatch.setattr(storage, "RESEARCH_DIR", str(tmp_path / "research"))
    monkeypatch.setattr(storage, "RESEARCH_CHECKPOINTS_DIR",
                        str(tmp_path / "research" / "checkpoints"))
    monkeypatch.setattr(storage, "LLMSTXT_DIR", str(tmp_path / "llmstxt"))

    paths = storage.save_llmstxt("https://site.test", "# T\n", "# T\nfull\n")
    assert paths["llmstxt_path"].startswith("data/llmstxt/")
    assert paths["llmstxt_path"].endswith("-llms.txt")
    assert paths["llms_full_path"].endswith("-llms-full.txt")
    files = sorted(p.name for p in (tmp_path / "llmstxt").iterdir())
    assert len(files) == 2


def test_llmstxt_job_status_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "LLMSTXT_DIR", str(tmp_path / "llmstxt"))
    job_id = str(uuid4())
    storage.save_llmstxt_job_status(job_id, {
        "status": "ready",
        "paths": {"llmstxt_path": "data/llmstxt/x-llms.txt"},
    })
    loaded = storage.load_llmstxt_job_status(job_id)
    assert loaded["status"] == "ready"
    assert loaded["paths"]["llmstxt_path"] == "data/llmstxt/x-llms.txt"


def _client():
    from app.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")


async def test_llmstxt_api_flow(tmp_path, monkeypatch):
    from app.crawl import repository as crawl_repository
    from app.crawl.service import crawl_service

    monkeypatch.setattr(storage, "LLMSTXT_DIR", str(tmp_path / "llmstxt"))
    job_id = uuid4()
    durable_job = {
        "id": job_id,
        "state": "completed",
        "config": {"url": "https://s.test"},
        "discovered_count": 1,
        "terminal_count": 1,
        "succeeded_count": 1,
        "results": [{
            "final_url": "https://s.test/",
            "title": "Home",
            "markdown": "# body",
            "metadata": {"description": "front door"},
        }],
    }

    async def fake_submit(config, **kwargs):
        assert config.url == "https://s.test"
        assert config.limit == 5
        assert config.maxDepth == 2
        return job_id

    async def fake_get_job(jid):
        assert jid == job_id
        return durable_job

    async def fake_wait(jid, **kw):
        return durable_job

    def fake_save(url, txt, full):
        (tmp_path / "llmstxt").mkdir(exist_ok=True)
        (tmp_path / "llmstxt" / "stem-llms.txt").write_text(txt)
        (tmp_path / "llmstxt" / "stem-llms-full.txt").write_text(full)
        return {"llmstxt_path": "data/llmstxt/stem-llms.txt",
                "llms_full_path": "data/llmstxt/stem-llms-full.txt"}

    monkeypatch.setattr(crawl_service, "submit_crawl", fake_submit)
    monkeypatch.setattr(crawl_repository, "get_job", fake_get_job)
    monkeypatch.setattr(storage, "save_llmstxt", fake_save)
    monkeypatch.setattr(llmstxt, "wait_for_terminal_job", fake_wait)

    async with _client() as c:
        resp = await c.post("/api/llmstxt",
                            json={"url": "https://s.test", "maxUrls": 5})
        assert resp.status_code == 202
        assert resp.json()["jobId"] == str(job_id)

        for _ in range(100):
            poll = (await c.get(f"/api/llmstxt/{job_id}")).json()
            if poll.get("llmstxt"):
                break
            await asyncio.sleep(0.01)

    assert poll["status"] == "completed"
    assert poll["pagesProcessed"] == 1
    assert poll["llmstxt"].startswith("# Home")
    assert "- [Home](https://s.test/): front door" in poll["llmstxt"]
    assert poll["llmstxtPath"] == "data/llmstxt/stem-llms.txt"
    assert poll["llmsFullPath"] == "data/llmstxt/stem-llms-full.txt"


async def test_llmstxt_api_validation():
    async with _client() as c:
        assert (await c.post("/api/llmstxt",
                             json={"url": "ftp://x"})).status_code == 400
        assert (await c.post("/api/llmstxt",
                             json={"url": "https://x.test",
                                   "maxUrls": 500})).status_code == 422


async def test_llmstxt_requires_db_and_404(monkeypatch):
    from app.crawl.service import crawl_service
    from app.crawl import repository as crawl_repository
    from app.crawl.repository import PersistenceUnavailable

    async def unavailable(*a, **k):
        raise PersistenceUnavailable("no db")

    async def missing(jid):
        return None

    monkeypatch.setattr(crawl_service, "submit_crawl", unavailable)
    monkeypatch.setattr(crawl_repository, "get_job", missing)

    async with _client() as c:
        resp = await c.post("/api/llmstxt", json={"url": "https://x.test"})
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "persistence_unavailable"
        assert (await c.get(f"/api/llmstxt/{uuid4()}")).status_code == 404
        assert (await c.get("/api/llmstxt/nope")).status_code == 404


async def test_llmstxt_generation_error_reported(tmp_path, monkeypatch):
    job_id = uuid4()
    durable_job = {
        "id": job_id,
        "state": "completed",
        "config": {"url": "https://s.test"},
        "results": [{
            "final_url": "https://s.test/",
            "title": "Home",
            "markdown": "# body",
            "metadata": {},
        }],
    }
    monkeypatch.setattr(storage, "LLMSTXT_DIR", str(tmp_path / "llmstxt"))

    async def fake_wait(jid, **kw):
        return durable_job

    monkeypatch.setattr(llmstxt, "wait_for_terminal_job", fake_wait)
    monkeypatch.setattr(
        storage, "save_llmstxt",
        lambda *a: (_ for _ in ()).throw(OSError("disk full")),
    )

    await llmstxt.run_for_job(job_id)
    status = storage.load_llmstxt_job_status(str(job_id))
    assert status["status"] == "error"
    assert "disk full" in status["error"]
