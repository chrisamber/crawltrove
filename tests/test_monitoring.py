"""Epic 2 — monitoring: health DB state, structured logging, signal-error mirror.

Backup scripts are shell and verified out-of-band (bash -n + a smoke run), not here.
"""
import logging
from pathlib import Path

import httpx

from tests.conftest import requires_db


def _client():
    from app.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")


async def test_public_version_comes_from_release_file():
    from app.main import VERSION, app
    release_version = (
        Path(__file__).resolve().parents[1] / "app" / "VERSION"
    ).read_text(encoding="utf-8").strip()

    async with _client() as c:
        health = await c.get("/api/health")
        openapi = await c.get("/openapi.json")

    assert VERSION == release_version
    assert health.json()["version"] == release_version
    assert openapi.json()["info"]["version"] == release_version


# --- /api/health db state (always 200, auth-exempt) --------------------------

async def test_health_reports_db_disabled(monkeypatch):
    from app.db import pool
    monkeypatch.delenv("DATABASE_URL", raising=False)
    await pool.reset_pool()
    async with _client() as c:
        r = await c.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "healthy"
    assert body["service"] == "crawltrove"
    assert body["db"] == "disabled"


@requires_db
async def test_health_reports_db_up(db):
    async with _client() as c:
        r = await c.get("/api/health")
    assert r.status_code == 200
    assert r.json()["db"] == "up"


# --- structured logging config ------------------------------------------------

def test_configure_logging_sets_level_and_is_idempotent(monkeypatch):
    from app import log as applog
    root = logging.getLogger()
    orig_handlers, orig_level, orig_flag = root.handlers[:], root.level, applog._configured
    try:
        applog._configured = False
        monkeypatch.setenv("LOG_LEVEL", "WARNING")
        applog.configure_logging()
        assert root.level == logging.WARNING
        n = len(root.handlers)
        applog.configure_logging()                 # second call: no-op
        assert len(root.handlers) == n
    finally:
        root.handlers, root.level, applog._configured = orig_handlers, orig_level, orig_flag


# --- swallowed signal errors mirrored into scrape_errors ----------------------

def test_build_result_captures_signal_error(monkeypatch):
    """A signal that raises must not fail the scrape; it's flagged in metadata."""
    from app import quality
    from app.scraper import WebScraper

    def boom(_):
        raise RuntimeError("quality exploded")

    monkeypatch.setattr(quality, "assess", boom)
    r = WebScraper()._build_result(
        "<html><body><p>hello world</p></body></html>",
        "https://e.com", True, "http")
    assert r["success"] is True                    # resilience preserved
    sigs = r["metadata"].get("signal_errors") or []
    assert any(s["signal"] == "quality" for s in sigs)


@requires_db
async def test_signal_errors_mirrored_to_scrape_errors(db, monkeypatch, tmp_path):
    from app import runner, storage, dedup

    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(storage, "save_scrape", lambda r: "sigstem")
    monkeypatch.setattr(dedup, "check_and_register",
                        lambda text, key: {"content_hash": "x"})

    scraped = {
        "url": "https://e.com/sig", "markdown": "hi",
        "metadata": {"engine": "http",
                     "signal_errors": [{"signal": "quality", "message": "boom"}]},
    }
    await runner.persist_scrape_page(scraped, {}, trigger="manual")

    async with db.acquire() as conn:
        errs = await conn.fetch(
            "SELECT * FROM scrape_errors WHERE stage = 'signal:quality'")
    assert len(errs) == 1
    assert errs[0]["page_url"] == "https://e.com/sig"
    assert "boom" in errs[0]["message"]
