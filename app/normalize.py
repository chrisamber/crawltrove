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
import ipaddress
import re
from typing import Any, Collection, Dict, List, Optional
from urllib.parse import unquote_plus, urlsplit, urlunsplit


MAX_URL_BYTES = 4096
_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
_PERCENT_ESCAPE = re.compile(r"%([0-9a-fA-F]{2})")


def _normalize_percent_encoding(value: str) -> str:
    """Decode unreserved characters and uppercase every other valid escape."""
    def replace(match: re.Match[str]) -> str:
        byte = int(match.group(1), 16)
        character = chr(byte)
        if character in _UNRESERVED:
            return character
        return f"%{byte:02X}"

    return _PERCENT_ESCAPE.sub(replace, value)


def _remove_dot_segments(path: str) -> str:
    """Resolve RFC 3986 dot segments without changing path case."""
    def remove_last_segment(value: str) -> str:
        boundary = value.rfind("/")
        return value[:boundary] if boundary >= 0 else ""

    remaining = path
    output = ""
    while remaining:
        if remaining.startswith("../"):
            remaining = remaining[3:]
        elif remaining.startswith("./"):
            remaining = remaining[2:]
        elif remaining.startswith("/./"):
            remaining = "/" + remaining[3:]
        elif remaining == "/.":
            remaining = "/"
        elif remaining.startswith("/../"):
            remaining = "/" + remaining[4:]
            output = remove_last_segment(output)
        elif remaining == "/..":
            remaining = "/"
            output = remove_last_segment(output)
        elif remaining in (".", ".."):
            remaining = ""
        else:
            boundary = remaining.find("/", 1 if remaining.startswith("/") else 0)
            if boundary < 0:
                output += remaining
                remaining = ""
            else:
                output += remaining[:boundary]
                remaining = remaining[boundary:]
    return output


def _normalized_host(host: str) -> Optional[str]:
    host = host.rstrip(".").lower()
    if not host:
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            return host.encode("idna").decode("ascii").lower()
        except UnicodeError:
            return None
    return address.compressed


def _serialized_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def normalize_url(
    url: str,
    *,
    tracking_parameters: Optional[Collection[str]] = None,
) -> str:
    """Return a deterministic URL identity without changing query semantics.

    Invalid inputs remain total and return a string. URLs that parse as absolute
    URLs but exceed the crawl identity limit return an empty string so callers
    cannot enqueue them. Tracking keys are removed only when the operator passes
    an explicit collection; remaining parameters keep their order and repeats.

    The root and trailing-slash serialization intentionally preserves the
    pre-v0.4 identity used by checkpoints and change tracking.
    """
    original = "" if url is None else str(url)
    if not original:
        return ""
    try:
        if len(original.encode("utf-8")) > MAX_URL_BYTES:
            return ""
    except UnicodeError:
        return ""
    try:
        parsed = urlsplit(original)
        scheme = parsed.scheme.lower()
        host = _normalized_host(parsed.hostname or "")
        if not scheme or host is None:
            return original
        port = parsed.port

        netloc = _serialized_host(host)
        default_port = (
            443 if scheme == "https" else 80 if scheme == "http" else None
        )
        if port is not None and port != default_port:
            netloc = f"{netloc}:{port}"

        path = _remove_dot_segments(_normalize_percent_encoding(parsed.path or "/"))
        # Existing public identity treats `/x` and `/x/`, and the two root
        # spellings, as the same page. Keep that compatibility for old artifacts.
        path = path.rstrip("/")

        query_parts = parsed.query.split("&") if parsed.query else []
        ignored = {str(name).casefold() for name in (tracking_parameters or ())}
        if ignored:
            query_parts = [
                part for part in query_parts
                if unquote_plus(part.partition("=")[0]).casefold() not in ignored
            ]
        query = _normalize_percent_encoding("&".join(query_parts))

        normalized = urlunsplit((scheme, netloc, path, query, ""))
        if len(normalized.encode("utf-8")) > MAX_URL_BYTES:
            return ""
        return normalized
    except Exception:
        return original


def origin_key(url: str) -> str:
    """Return `scheme://idna-host:effective-port` for an HTTP(S) URL."""
    try:
        normalized = normalize_url(url)
        parsed = urlsplit(normalized)
        scheme = parsed.scheme.lower()
        host = _normalized_host(parsed.hostname or "")
        if scheme not in ("http", "https") or host is None:
            return ""
        port = parsed.port or (443 if scheme == "https" else 80)
        return f"{scheme}://{_serialized_host(host)}:{port}"
    except Exception:
        return ""


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
