"""llms.txt generation: pure format, storage pair, and the 202+poll API flow."""
import asyncio

import httpx
import pytest

from app import llmstxt, storage
from app.services import crawler


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


@pytest.fixture(autouse=True)
def _clean_jobs():
    crawler.jobs.clear()
    yield
    crawler.jobs.clear()


def _client():
    from app.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")


async def test_llmstxt_api_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "LLMSTXT_DIR", str(tmp_path / "llmstxt"))

    async def fake_run_crawl(job_id, **kw):
        job = crawler.get_job(job_id)
        job["status"] = "completed"
        job["progress"] = 1.0
        job["results"] = [_page("https://s.test/", "Home", "front door")]

    monkeypatch.setattr(crawler, "run_crawl", fake_run_crawl)

    def fake_save(url, txt, full):
        (tmp_path / "llmstxt").mkdir(exist_ok=True)
        (tmp_path / "llmstxt" / "stem-llms.txt").write_text(txt)
        (tmp_path / "llmstxt" / "stem-llms-full.txt").write_text(full)
        return {"llmstxt_path": "data/llmstxt/stem-llms.txt",
                "llms_full_path": "data/llmstxt/stem-llms-full.txt"}

    monkeypatch.setattr(storage, "save_llmstxt", fake_save)

    async with _client() as c:
        resp = await c.post("/api/llmstxt",
                            json={"url": "https://s.test", "maxUrls": 5})
        assert resp.status_code == 202
        job_id = resp.json()["jobId"]
        # crawler.create_job used our params
        assert crawler.get_job(job_id)["limit"] == 5
        assert crawler.get_job(job_id)["max_depth"] == 2

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
        assert (await c.get("/api/llmstxt/nope")).status_code == 404


async def test_llmstxt_generation_error_reported(monkeypatch):
    async def fake_run_crawl(job_id, **kw):
        job = crawler.get_job(job_id)
        job["status"] = "completed"
        job["results"] = [_page("https://s.test/", "Home")]

    monkeypatch.setattr(crawler, "run_crawl", fake_run_crawl)
    monkeypatch.setattr(storage, "save_llmstxt",
                        lambda *a: (_ for _ in ()).throw(OSError("disk full")))

    job_id = crawler.create_job(base_url="https://s.test", limit=2)
    await llmstxt.run(crawler, job_id)
    job = crawler.get_job(job_id)
    assert "disk full" in job["llmstxt_error"]
    assert "llmstxt" not in job
