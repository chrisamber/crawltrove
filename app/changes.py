"""Change-tracking: compare each scrape of a URL against its previous scrape.

One more per-page corpus signal: the report
is `{previousScrapeAt, previousContentHash, changeStatus: new|same|changed}`
and lands in metadata.changeTracking. Flags only — nothing is dropped or
short-circuited on "same"; the consumer decides.

History lives in DATA_DIR/index/url_history.json (latest content_hash + time
per normalized URL, atomic writes, single-process like dedup). When a URL has
no file entry and Postgres is enabled, the newest scraped_pages row for that
URL seeds the comparison (covers scrapes indexed before this signal existed).
Any failure returns None — a change-tracking error never touches a scrape.
"""
import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Dict, Optional

from app.normalize import normalize_url
from app.storage import DATA_DIR

logger = logging.getLogger("changes")

INDEX_DIR = os.path.join(DATA_DIR, "index")
HISTORY_PATH = os.path.join(INDEX_DIR, "url_history.json")

_lock = threading.Lock()
_history: Optional[Dict[str, Dict[str, str]]] = None


def _load() -> None:
    global _history
    if _history is not None:
        return
    os.makedirs(INDEX_DIR, exist_ok=True)
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            _history = json.load(f)
        if not isinstance(_history, dict):
            _history = {}
    except Exception:
        _history = {}


def _save() -> None:
    tmp = HISTORY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_history, f)
    os.replace(tmp, HISTORY_PATH)


async def _db_previous(url: str) -> Optional[Dict[str, str]]:
    """Newest indexed page for this URL, as a history entry. None when the DB
    is disabled, unreachable, or has never seen the URL (all swallowed)."""
    try:
        from app.db import repo
        row = await repo.get_last_page_by_url(url)
        if row and row.get("content_hash"):
            created = row.get("created_at")
            return {
                "content_hash": row["content_hash"],
                "scraped_at": created.isoformat() if created else None,
            }
    except Exception as e:
        logger.warning("change-tracking DB fallback failed for %s: %s", url, e)
    return None


async def check_and_register(url: str, content_hash: Optional[str]) -> Optional[Dict]:
    """Compare against the URL's previous scrape, then record this one.

    Returns the changeTracking report, or None when the signal can't run
    (missing hash, storage failure) — never raises into a scrape path.
    """
    try:
        if not url or not content_hash:
            return None
        key = normalize_url(url)

        with _lock:
            _load()
            previous = _history.get(key)
        if previous is None:
            # First sight in the file index — the DB may still know the URL.
            # Awaited outside the lock so a slow DB never blocks other scrapes.
            previous = await _db_previous(url)

        now = datetime.now(timezone.utc).isoformat()
        with _lock:
            _load()
            _history[key] = {"content_hash": content_hash, "scraped_at": now}
            _save()

        if previous is None:
            return {"previousScrapeAt": None, "previousContentHash": None,
                    "changeStatus": "new"}
        return {
            "previousScrapeAt": previous.get("scraped_at"),
            "previousContentHash": previous.get("content_hash"),
            "changeStatus": ("same" if previous.get("content_hash") == content_hash
                             else "changed"),
        }
    except Exception as e:
        logger.warning("change-tracking failed for %s: %s", url, e)
        return None
