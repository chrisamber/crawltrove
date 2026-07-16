"""Change-tracking signal: new → same → changed, resilience, and wiring.

The file store is redirected to a tmp path per test; the DB fallback is
exercised with mocks (and a real roundtrip under @requires_db).
"""
import httpx
import pytest

from app import changes
from tests.conftest import requires_db


@pytest.fixture(autouse=True)
def _tmp_history(tmp_path, monkeypatch):
    monkeypatch.setattr(changes, "INDEX_DIR", str(tmp_path))
    monkeypatch.setattr(changes, "HISTORY_PATH", str(tmp_path / "url_history.json"))
    monkeypatch.setattr(changes, "_history", None)
    yield


async def test_new_same_changed_sequence():
    url = "https://example.com/page"
    first = await changes.check_and_register(url, "hash-a")
    assert first == {"previousScrapeAt": None, "previousContentHash": None,
                     "changeStatus": "new"}

    second = await changes.check_and_register(url, "hash-a")
    assert second["changeStatus"] == "same"
    assert second["previousContentHash"] == "hash-a"
    assert second["previousScrapeAt"] is not None

    third = await changes.check_and_register(url, "hash-b")
    assert third["changeStatus"] == "changed"
    assert third["previousContentHash"] == "hash-a"


async def test_url_identity_is_normalized():
    await changes.check_and_register("https://Example.com/page/", "hash-a")
    again = await changes.check_and_register("https://example.com/page", "hash-a")
    assert again["changeStatus"] == "same"


async def test_missing_hash_returns_none():
    assert await changes.check_and_register("https://example.com", None) is None
    assert await changes.check_and_register("", "hash-a") is None


async def test_corrupt_index_file_degrades_to_new(tmp_path):
    with open(changes.HISTORY_PATH, "w", encoding="utf-8") as f:
        f.write("{not json")
    report = await changes.check_and_register("https://example.com", "hash-a")
    assert report["changeStatus"] == "new"


async def test_save_failure_returns_none(monkeypatch):
    def broken_save():
        raise OSError("disk full")
    monkeypatch.setattr(changes, "_save", broken_save)
    assert await changes.check_and_register("https://example.com", "h") is None


async def test_db_fallback_seeds_history(monkeypatch):
    import datetime

    async def fake_last(url):
        return {"content_hash": "hash-old",
                "created_at": datetime.datetime(2026, 1, 2, tzinfo=datetime.timezone.utc)}

    from app.db import repo
    monkeypatch.setattr(repo, "get_last_page_by_url", fake_last)
    report = await changes.check_and_register("https://example.com/p", "hash-new")
    assert report["changeStatus"] == "changed"
    assert report["previousContentHash"] == "hash-old"
    assert report["previousScrapeAt"].startswith("2026-01-02")

    # The file entry now exists; the DB must not be consulted again.
    async def exploding(url):
        raise AssertionError("DB fallback must not run when the file knows the URL")
    monkeypatch.setattr(repo, "get_last_page_by_url", exploding)
    again = await changes.check_and_register("https://example.com/p", "hash-new")
    assert again["changeStatus"] == "same"


async def test_scrape_response_carries_change_tracking(monkeypatch):
    """/api/scrape metadata gains changeTracking via persist_scrape_page."""
    import app.services as services
    from app import dedup, storage

    async def fake_scrape(url, **kw):
        return {"success": True, "url": url, "title": "t", "description": "",
                "markdown": "# body", "html": "<html/>",
                "metadata": {"url": url, "engine": "http"}}

    monkeypatch.setattr(services.scraper, "scrape", fake_scrape)
    monkeypatch.setattr(
        dedup, "check_and_register",
        lambda text, key: {"content_hash": "abc",
                           "exact_duplicate_of": None, "near_duplicate_of": None})
    monkeypatch.setattr(storage, "save_scrape", lambda result: "stem")
    monkeypatch.setattr(storage, "save_run_raw", lambda *a, **kw: {})

    from app.main import app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        first = await c.post("/api/scrape", json={"url": "https://example.com/x"})
        second = await c.post("/api/scrape", json={"url": "https://example.com/x"})

    assert first.json()["metadata"]["changeTracking"]["changeStatus"] == "new"
    assert second.json()["metadata"]["changeTracking"]["changeStatus"] == "same"


@requires_db
async def test_db_fallback_roundtrip(db):
    """With a real DB row and an empty file index, the previous scrape is found."""
    from app.db import repo

    run_id = await repo.record_run_start(external_id="ct-run", trigger="manual",
                                         status="processing")
    await repo.record_page(run_id, url="https://example.com/db", status_code=200,
                           engine="http", extractor="trafilatura",
                           content_hash="hash-db", extracted_text="x",
                           raw_json_path=None, raw_md_path=None,
                           raw_html_path=None, metadata={})

    report = await changes.check_and_register("https://example.com/db", "hash-db")
    assert report["changeStatus"] == "same"
    assert report["previousContentHash"] == "hash-db"
