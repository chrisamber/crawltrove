"""Shared helpers for the Epic 3 S5 corpus collectors.

Each collector discovers pages, turns them into the standard crawl-artifact
shape (via app.corpus.crawl_shape), and writes them to data/crawls/ so
scripts/build_corpus.py --from-crawl can route + chunk + tier them. The pure
parsing/selection helpers live in each collector module and are unit-tested; the
network layer here is thin and injectable (pass a fake `fetch`/`scrape` in tests).
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import storage  # noqa: E402
from app.corpus import crawl_shape  # noqa: E402

DEFAULT_HEADERS = {
    "User-Agent": "crawltrove-corpus-collector/1.0",
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
}


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def http_get(url: str, *, headers: Optional[Dict[str, str]] = None, timeout: int = 30):
    """GET with Chrome TLS impersonation (matches the project's fetch.py style)."""
    from curl_cffi import requests as cffi_requests
    resp = cffi_requests.get(url, headers=headers or DEFAULT_HEADERS,
                             impersonate="chrome110", timeout=timeout)
    resp.raise_for_status()
    return resp


def get_json(url: str, **kw) -> Any:
    return http_get(url, **kw).json()


def get_text(url: str, **kw) -> str:
    return http_get(url, **kw).text


def make_result(url: str, title: str, markdown: str, **meta: Any) -> Dict[str, Any]:
    """One crawl result row: {url, markdown, metadata:{title, ...meta}}."""
    m: Dict[str, Any] = {"title": title}
    m.update({k: v for k, v in meta.items() if v is not None})
    return {"url": url, "markdown": markdown, "metadata": m}


def write_artifact(results: List[Dict[str, Any]], *, base_url: str, source: str,
                   out: str = "", now: str = "") -> str:
    """Write the crawl artifact. With `out`, write JSON to that exact path;
    otherwise auto-stem via storage.save_crawl. Returns the artifact path."""
    job = crawl_shape.job_from_results(results, base_url=base_url, source=source,
                                       now=now or now_iso())
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        return out
    stem = storage.save_crawl(job)
    return os.path.join(storage.CRAWLS_DIR, stem + ".json")


async def default_scrape(url: str) -> Optional[Dict[str, Any]]:
    """Scrape one URL through the service waterfall → a crawl result row (or None
    on failure). Used by the HTML collectors; tests inject a fake instead."""
    from app.services import scraper
    try:
        res = await scraper.scrape(url=url, only_main_content=True, engine="auto")
    except Exception:
        return None
    if not res.get("success") or not res.get("markdown"):
        return None
    res.pop("_raw", None)
    meta = res.get("metadata") or {}
    return make_result(res["url"], res.get("title", ""), res["markdown"],
                       license=meta.get("license"), quality=meta.get("quality"),
                       language=meta.get("language"))
