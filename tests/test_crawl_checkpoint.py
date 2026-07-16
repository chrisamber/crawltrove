"""Crawl checkpoint/resume — mirrors tests/test_research_checkpoint.py.

The scraper is faked (counting calls), the sitemap returns fixture URLs, and
checkpoints go to a tmp DATA_DIR. A "crash" is simulated by cancelling the
run_crawl task and its workers (research's raise-KeyboardInterrupt trick
would escape the crawler's worker tasks and abort the whole pytest session);
the restart simulation builds a FRESH WebCrawler over the same checkpoint dir.
"""
import asyncio
import json

import httpx
import pytest

import app.crawler as crawler_mod
from app import changes, sitemap, storage
from app.crawler import WebCrawler


@pytest.fixture
def tmp_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(storage, "SCRAPES_DIR", str(tmp_path / "scrapes"))
    monkeypatch.setattr(storage, "CRAWLS_DIR", str(tmp_path / "crawls"))
    monkeypatch.setattr(storage, "RESEARCH_DIR", str(tmp_path / "research"))
    monkeypatch.setattr(storage, "RESEARCH_CHECKPOINTS_DIR",
                        str(tmp_path / "research" / "checkpoints"))
    monkeypatch.setattr(storage, "CRAWL_CHECKPOINTS_DIR",
                        str(tmp_path / "crawls" / "checkpoints"))
    monkeypatch.setattr(storage, "LLMSTXT_DIR", str(tmp_path / "llmstxt"))
    monkeypatch.setattr(changes, "INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.setattr(changes, "HISTORY_PATH", str(tmp_path / "index" / "u.json"))
    monkeypatch.setattr(changes, "_history", None)
    return tmp_path


PAGES = [f"https://s.test/p{i}" for i in range(4)]


@pytest.fixture
def env(tmp_dirs, monkeypatch):
    """WebCrawler factory: instant fake scrapes, sitemap seeds PAGES."""
    # No checkpoint throttling in tests — every page persists.
    monkeypatch.setattr(crawler_mod, "CHECKPOINT_INTERVAL_S", 0.0)

    async def fake_discover(base_url, cap=200):
        return list(PAGES)

    monkeypatch.setattr(sitemap, "discover", fake_discover)

    def build(hang_after=None, with_screenshots=False):
        c = WebCrawler()
        scraped = []

        async def fake_scrape(url, **kw):
            if hang_after is not None and len(scraped) >= hang_after:
                await asyncio.sleep(3600)   # in-flight work at "crash" time
            scraped.append(url)
            r = {"success": True, "url": url, "title": "T", "description": "",
                 "markdown": f"# {url}", "html": "<html><body>x</body></html>",
                 "metadata": {"url": url, "engine": "http", "status_code": 200}}
            if with_screenshots:
                r["_raw"] = {"screenshot": b"\x89PNG"}
            return r

        c.scraper.scrape = fake_scrape
        return c, scraped

    return build


async def _crash_mid_crawl(c, job_id, scraped, after):
    """Start run_crawl, wait for `after` completed pages, then cancel every
    task we spawned — the closest an event loop gets to a SIGKILL."""
    run = asyncio.create_task(c.run_crawl(job_id))
    for _ in range(500):
        if len(scraped) >= after:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)          # let the hanging scrape be in flight
    doomed = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for t in doomed:
        t.cancel()
    await asyncio.gather(*doomed, return_exceptions=True)


def _checkpoint_path(tmp_dirs, job_id):
    return tmp_dirs / "crawls" / "checkpoints" / f"{job_id}.json"


async def test_checkpoint_written_and_deleted_at_terminal(env, tmp_dirs, monkeypatch):
    writes = []
    real_save = storage.save_crawl_checkpoint

    def spying_save(job_id, payload):
        writes.append(json.loads(json.dumps(payload)))
        real_save(job_id, payload)

    monkeypatch.setattr(storage, "save_crawl_checkpoint", spying_save)

    c, scraped = env()
    jid = c.create_job("https://s.test", limit=3, max_depth=0)
    await c.run_crawl(jid)

    job = c.get_job(jid)
    assert job["status"] == "completed"
    assert job["artifact_stem"]
    assert len(writes) >= 2                       # seed + per-page
    assert all(w["version"] == 1 for w in writes)
    assert set(writes[-1]["loop"].keys()) == {"visited", "pending", "shot_counter"}
    assert not _checkpoint_path(tmp_dirs, jid).exists()


async def test_crash_then_restart_rehydrates_as_interrupted(env, tmp_dirs):
    c, scraped = env(hang_after=2)
    jid = c.create_job("https://s.test", limit=10, max_depth=0)
    await _crash_mid_crawl(c, jid, scraped, after=2)
    assert len(scraped) == 2
    assert _checkpoint_path(tmp_dirs, jid).exists()

    c2 = WebCrawler()
    restored = c2.restore_from_checkpoints()
    assert restored == [jid]
    job = c2.get_job(jid)
    assert job["status"] == "interrupted"
    assert len(job["results"]) == 2               # pre-crash pages preserved
    # Interrupted jobs are idle; parked loop state awaits resume.
    assert jid in c2._pending_resume


async def test_resume_completes_without_rescraping(env, tmp_dirs):
    c, first_pass = env(hang_after=2)
    jid = c.create_job("https://s.test", limit=10, max_depth=0)
    await _crash_mid_crawl(c, jid, first_pass, after=2)

    c2, resumed = env()
    assert c2.restore_from_checkpoints() == [jid]
    assert c2.resume_crawl(jid) is True
    for t in list(c2._tasks):
        await t

    job = c2.get_job(jid)
    assert job["status"] == "completed"
    # Nothing scraped before the crash was scraped again...
    assert not set(first_pass) & set(resumed)
    # ...and the final results carry no duplicates.
    result_urls = [r["url"] for r in job["results"]]
    assert len(result_urls) == len(set(result_urls))
    assert set(u for u in first_pass) <= set(result_urls)
    assert job["artifact_stem"]
    assert not _checkpoint_path(tmp_dirs, jid).exists()


async def test_shot_counter_survives_resume(env, tmp_dirs):
    """Resumed screenshot numbering continues instead of overwriting page-1."""
    c, first = env(hang_after=1, with_screenshots=True)
    jid = c.create_job("https://s.test", limit=4, max_depth=0, screenshots=True)
    await _crash_mid_crawl(c, jid, first, after=1)
    ckpt = json.loads(_checkpoint_path(tmp_dirs, jid).read_text())
    assert ckpt["loop"]["shot_counter"] == 1

    c2, resumed = env(with_screenshots=True)
    c2.restore_from_checkpoints()
    c2.resume_crawl(jid)
    for t in list(c2._tasks):
        await t
    paths = [r["screenshot_path"] for r in c2.get_job(jid)["results"]
             if r.get("screenshot_path")]
    assert len(paths) >= 2
    assert len(paths) == len(set(paths))          # no overwritten page numbers


async def test_corrupt_checkpoint_skipped_and_terminal_cleaned(tmp_dirs):
    ckdir = tmp_dirs / "crawls" / "checkpoints"
    ckdir.mkdir(parents=True, exist_ok=True)
    (ckdir / "bad.json").write_text("{not json")
    storage.save_crawl_checkpoint("done-job", {
        "version": 1, "job": {"job_id": "done-job", "status": "completed"},
        "loop": {}, "updated_at": "2026-01-01T00:00:00+00:00"})

    c = WebCrawler()
    assert c.restore_from_checkpoints() == []
    assert not (ckdir / "done-job.json").exists()


async def test_resume_requires_interrupted(tmp_dirs):
    c = WebCrawler()
    jid = c.create_job("https://s.test")
    assert c.resume_crawl(jid) is False
    assert c.resume_crawl("unknown") is False


async def test_resume_endpoint_guards(monkeypatch):
    from app.services import crawler as singleton
    monkeypatch.setattr(singleton, "resume_crawl", lambda job_id: True)

    from app.main import app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as cli:
        assert (await cli.post("/api/crawl/nope/resume")).status_code == 404

        jid = singleton.create_job("https://s.test")
        assert (await cli.post(f"/api/crawl/{jid}/resume")).status_code == 409

        singleton.get_job(jid)["status"] = "interrupted"
        resp = await cli.post(f"/api/crawl/{jid}/resume")
        assert resp.status_code == 200
        assert resp.json()["success"] is True
    singleton.jobs.pop(jid, None)


async def test_checkpoint_throttle(env, monkeypatch):
    """With a real interval, per-page checkpoints collapse to the forced seed
    write only."""
    monkeypatch.setattr(crawler_mod, "CHECKPOINT_INTERVAL_S", 60.0)
    writes = []
    monkeypatch.setattr(storage, "save_crawl_checkpoint",
                        lambda job_id, payload: writes.append(1))

    c, scraped = env()
    jid = c.create_job("https://s.test", limit=4, max_depth=0)
    await c.run_crawl(jid)
    assert len(scraped) == 4
    assert len(writes) == 1


def test_prune_sweeps_stale_crawl_checkpoints(tmp_dirs):
    import os
    import time
    storage.save_crawl_checkpoint("stale", {"version": 1, "job": {}, "loop": {}})
    p = tmp_dirs / "crawls" / "checkpoints" / "stale.json"
    old = time.time() - 90 * 86400
    os.utime(p, (old, old))
    out = storage.prune(max_age_days=30, keep_runs=0)
    assert out["removed"] >= 1
    assert not p.exists()
