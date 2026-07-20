"""Shared, side-effect-free mappers between scrape/crawl/extract output and the
persistence layer's row shapes.

Three inlined mappers had drifted apart:
  * crawler._normalize_url            -> normalize_url()
  * runner.page_fields_from_scrape    -> page_row_from_result()
  * crawler._db_page_fields           -> page_row_from_crawl_item()

plus the new extract->records mapping (record_rows_from_extract). Keeping one
definition means the crawler frontier and the DB index agree on URL identity.

Contract: every function is total — plain dicts in, plain dicts out, **never
raises**, no imports of dedup/storage/DB (so it stays trivially testable and
safe to call from any path). content_hash for records is left None here; the
caller computes it (it owns the dedup index).
"""
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse


def normalize_url(url: str) -> str:
    """Canonical URL identity: lowercase host, no trailing slash, no fragment,
    query preserved (it is often part of page identity — search/pagination)."""
    try:
        parsed = urlparse(url or "")
        path = parsed.path.rstrip("/")
        query = f"?{parsed.query}" if parsed.query else ""
        return f"{parsed.scheme}://{parsed.netloc.lower()}{path}{query}"
    except Exception:
        return url or ""


def page_row_from_result(
    result: Dict[str, Any],
    stem: Optional[str],
    *,
    raw_html_path: Optional[str] = None,
    screenshot_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Map a WebScraper.scrape() result + storage stem to scraped_pages columns.

    status_code is promoted from metadata (the scraper threads it there);
    screenshot_path rides inside metadata (no dedicated column).
    """
    meta = dict(result.get("metadata") or {})   # copy: never mutate the caller's
    if screenshot_path:
        meta["screenshot_path"] = screenshot_path
    dedup_info = meta.get("dedup") or {}
    return {
        "url": result.get("url"),
        "status_code": meta.get("status_code"),
        "engine": meta.get("engine"),
        "extractor": meta.get("extractor"),
        "content_hash": dedup_info.get("content_hash"),
        "extracted_text": result.get("markdown"),
        "raw_json_path": (f"data/scrapes/{stem}.json" if stem else None),
        "raw_md_path": (f"data/scrapes/{stem}.md" if stem else None),
        "raw_html_path": raw_html_path,
        "metadata": meta,
    }


def page_row_from_crawl_item(
    item: Dict[str, Any],
    *,
    raw_html_path: Optional[str] = None,
    screenshot_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Map one crawl result item to scraped_pages columns.

    The crawl flattens each page (engine/extractor/license/... are siblings of
    `dedup`, not nested under `metadata`), so rebuild a normalized metadata block
    matching the shape the scrape path writes.
    """
    dedup_info = item.get("dedup") or {}
    metadata = {
        "title": item.get("title", ""),
        "description": item.get("description", ""),
        "url": item.get("url"),
        "engine": item.get("engine"),
        "extractor": item.get("extractor"),
        "license": item.get("license"),
        "quality": item.get("quality"),
        "language": item.get("language"),
        "status_code": item.get("status_code"),
        "dedup": dedup_info or None,
        "changeTracking": item.get("changeTracking"),
    }
    if screenshot_path:
        metadata["screenshot_path"] = screenshot_path
    return {
        "url": item.get("url"),
        "status_code": item.get("status_code"),
        "engine": item.get("engine"),
        "extractor": item.get("extractor"),
        "content_hash": dedup_info.get("content_hash"),
        "extracted_text": item.get("markdown"),
        "raw_html_path": raw_html_path,
        "metadata": metadata,
    }


def record_rows_from_extract(
    extracted: Optional[Dict[str, Any]],
    source_url: str,
    *,
    record_type: str = "extract",
) -> List[Dict[str, Any]]:
    """Map extract_llm.extract() output to extracted_records rows (one per record).

    A dict `data` yields one row; a list `data` yields one row per element.
    content_hash is left None — the caller computes it (it owns the dedup index).
    `confidence` is lifted from the record body when the model emitted one.
    """
    data = (extracted or {}).get("data")
    if data is None:
        return []
    items = data if isinstance(data, list) else [data]
    rows: List[Dict[str, Any]] = []
    for item in items:
        confidence = item.get("confidence") if isinstance(item, dict) else None
        rows.append({
            "source_url": source_url,
            "record_type": record_type,
            "data_json": item,
            "content_hash": None,
            "confidence": confidence,
        })
    return rows
