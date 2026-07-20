"""Tests for raw capture and retention.

All hermetic: scraper._build_result runs the real signal pipeline on a static
HTML string (no network); storage functions run against a tmp DATA_DIR. The DB
wiring is covered separately in test_api_integration.py.
"""
import os
import time

from app.scraper import WebScraper


# --- scraper._build_result: additive status_code + raw html ------------------

def test_build_result_threads_status_code_into_metadata():
    s = WebScraper()
    html = "<html><head><title>T</title></head><body><p>hello world</p></body></html>"
    r = s._build_result(html, "https://e.com", only_main_content=True,
                        engine_used="http", status_code=403)
    assert r["success"] is True
    assert r["metadata"]["status_code"] == 403


def test_build_result_exposes_raw_html_for_capture():
    s = WebScraper()
    html = "<html><body><p>verbatim &amp; raw</p></body></html>"
    r = s._build_result(html, "https://e.com", True, "http")
    # Raw, pre-clean HTML is available on a private channel for persistence,
    # distinct from the cleaned `html` field already returned.
    assert r["_raw"]["html"] == html
    assert r["metadata"]["status_code"] is None   # default when not threaded


def test_build_result_preserves_license_before_clean():
    """License markers live in footers (stripped by cleaning) — detection must
    still see the raw HTML. Guards the invariant against the raw-capture change."""
    s = WebScraper()
    html = (
        "<html><body><main><p>" + ("content " * 60) + "</p></main>"
        "<footer>Licensed under CC BY 4.0 "
        "<a href='https://creativecommons.org/licenses/by/4.0/'>license</a>"
        "</footer></body></html>"
    )
    r = s._build_result(html, "https://e.com", True, "http")
    assert r["metadata"]["license"] is not None


def test_clean_and_convert_preserves_basic_markdown_structure():
    s = WebScraper()
    html = (
        "<main><h1>Guide</h1><p>Visit "
        "<a href='/docs'>the docs</a>.</p><ul><li>One</li><li>Two</li></ul></main>"
    )
    _, markdown = s.clean_and_convert(html, "https://example.com", False)
    assert "# Guide" in markdown
    assert "[the docs](https://example.com/docs)" in markdown
    assert "* One" in markdown or "- One" in markdown


# --- storage.save_run_raw ----------------------------------------------------

def test_save_run_raw_writes_html_and_screenshot(tmp_path, monkeypatch):
    from app import storage
    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))

    paths = storage.save_run_raw("stem1", 1, raw_html="<html>x</html>",
                                 screenshot=b"\x89PNG\r\n")
    assert paths["raw_html_path"] == "data/runs/stem1/page-1.html.txt"
    assert paths["screenshot_path"] == "data/runs/stem1/page-1.png"
    html_file = tmp_path / "runs" / "stem1" / "page-1.html.txt"
    png_file = tmp_path / "runs" / "stem1" / "page-1.png"
    assert html_file.read_text() == "<html>x</html>"
    assert png_file.read_bytes() == b"\x89PNG\r\n"


def test_save_run_raw_html_only(tmp_path, monkeypatch):
    from app import storage
    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    paths = storage.save_run_raw("s2", 1, raw_html="<p>a</p>")
    assert paths == {"raw_html_path": "data/runs/s2/page-1.html.txt"}


async def test_legacy_raw_html_is_served_as_plain_attachment(tmp_path):
    import httpx
    from fastapi import FastAPI
    from app.main import ArtifactStaticFiles

    (tmp_path / "page-1.html").write_text("<script>alert(1)</script>")
    app = FastAPI()
    app.mount("/", ArtifactStaticFiles(directory=str(tmp_path)))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/page-1.html")
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["x-content-type-options"] == "nosniff"


def test_save_run_raw_no_stem_or_nothing_is_noop(tmp_path, monkeypatch):
    from app import storage
    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    assert storage.save_run_raw(None, 1, raw_html="x") == {}
    assert storage.save_run_raw("s3", 1) == {}
    assert not (tmp_path / "runs").exists()


# --- crawl screenshots (opt-in) -----------------------------------------------

async def _run_crawl(tmp_path, monkeypatch, *, screenshots, with_shot=True):
    from app import changes, sitemap, storage
    from app.crawler import WebCrawler

    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(changes, "INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.setattr(changes, "HISTORY_PATH", str(tmp_path / "index" / "u.json"))
    monkeypatch.setattr(changes, "_history", None)
    monkeypatch.setattr(storage, "save_crawl", lambda job: "crawlstem")

    async def no_sitemap(base_url, cap=200):
        return []

    monkeypatch.setattr(sitemap, "discover", no_sitemap)

    crawler = WebCrawler()

    async def fake_scrape(url, **kw):
        r = {"success": True, "url": url, "title": "T", "description": "",
             "markdown": "# body", "html": "<html><body>x</body></html>",
             "metadata": {"url": url, "engine": "browser", "status_code": 200}}
        if with_shot:
            r["_raw"] = {"html": "<html/>", "screenshot": b"\x89PNG\r\nshot"}
        return r

    crawler.scraper.scrape = fake_scrape
    job_id = crawler.create_job("https://s.test", limit=1, max_depth=0,
                                screenshots=screenshots)
    await crawler.run_crawl(job_id)
    return crawler.get_job(job_id), job_id


async def test_crawl_screenshots_opt_in_saves_png(tmp_path, monkeypatch):
    job, job_id = await _run_crawl(tmp_path, monkeypatch, screenshots=True)
    assert job["status"] == "completed"
    item = job["results"][0]
    assert item["screenshot_path"] == f"data/runs/{job_id}/page-1.png"
    png = tmp_path / "runs" / job_id / "page-1.png"
    assert png.read_bytes() == b"\x89PNG\r\nshot"
    # Bytes never enter the job dict — only the path does.
    assert "_raw" not in item
    from app import normalize
    row = normalize.page_row_from_crawl_item(
        item, screenshot_path=item.get("screenshot_path"))
    assert row["metadata"]["screenshot_path"] == item["screenshot_path"]


async def test_crawl_screenshots_default_off(tmp_path, monkeypatch):
    job, job_id = await _run_crawl(tmp_path, monkeypatch, screenshots=False)
    assert job["results"][0]["screenshot_path"] is None
    assert not (tmp_path / "runs").exists()


async def test_crawl_screenshots_tier1_page_has_none(tmp_path, monkeypatch):
    """HTTP-tier pages produce no screenshot; the crawl still succeeds."""
    job, job_id = await _run_crawl(tmp_path, monkeypatch,
                                   screenshots=True, with_shot=False)
    assert job["status"] == "completed"
    assert job["results"][0]["screenshot_path"] is None


# --- storage.prune (retention) ----------------------------------------------

def _write(path, content="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def test_prune_removes_old_keeps_recent_and_spares_index(tmp_path, monkeypatch):
    from app import storage
    data = str(tmp_path)
    monkeypatch.setattr(storage, "DATA_DIR", data)
    monkeypatch.setattr(storage, "SCRAPES_DIR", os.path.join(data, "scrapes"))
    monkeypatch.setattr(storage, "CRAWLS_DIR", os.path.join(data, "crawls"))
    monkeypatch.setattr(storage, "RESEARCH_DIR", os.path.join(data, "research"))
    monkeypatch.setattr(storage, "RESEARCH_CHECKPOINTS_DIR",
                        os.path.join(data, "research", "checkpoints"))

    old = time.time() - 40 * 86400
    # Two old scrapes (json+md pairs) and one fresh one.
    for stem in ("old1", "old2"):
        for ext in (".json", ".md"):
            p = os.path.join(data, "scrapes", stem + ext)
            _write(p)
            os.utime(p, (old, old))
    for ext in (".json", ".md"):
        _write(os.path.join(data, "scrapes", "fresh" + ext))
    # An old + a fresh run subdir, plus the dedup index (never pruned).
    old_run = os.path.join(data, "runs", "oldrun")
    _write(os.path.join(old_run, "page-1.html"))
    os.utime(old_run, (old, old))
    fresh_run = os.path.join(data, "runs", "freshrun")
    _write(os.path.join(fresh_run, "page-1.html"))
    _write(os.path.join(data, "index", "exact_hashes.json"))

    report = storage.prune(max_age_days=30, keep_runs=1)

    # keep_runs=1 spares the single newest entry per kind; older ones past the
    # age cutoff are removed.
    assert os.path.exists(os.path.join(data, "scrapes", "fresh.json"))
    assert not os.path.exists(os.path.join(data, "scrapes", "old1.json"))
    assert not os.path.exists(os.path.join(data, "scrapes", "old2.md"))
    assert os.path.exists(fresh_run)                         # newest run kept
    assert not os.path.exists(old_run)                       # old run dir removed
    assert os.path.exists(os.path.join(data, "index", "exact_hashes.json"))  # spared
    assert report["removed"] >= 3
