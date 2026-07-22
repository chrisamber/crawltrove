"""Deterministic, side-effect-free discovery for durable crawl pages."""

from collections import Counter
from dataclasses import dataclass
import re
from typing import Any, Literal, Mapping, MutableMapping, Optional, Sequence
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from app.crawl.config import CrawlConfig
from app.normalize import MAX_URL_BYTES, normalize_url, origin_key


_DOCUMENT_SUFFIXES = (".pdf", ".epub")
_IMAGE_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp", ".gif",
)
_SKIPPED_SUFFIXES = (
    ".7z", ".avi", ".css", ".csv", ".dmg", ".doc", ".docx", ".exe",
    ".gz", ".ico", ".iso", ".js", ".json", ".m4a", ".m4v", ".mov",
    ".mp3", ".mp4", ".mpeg", ".mpg", ".ppt", ".pptx", ".rar", ".rss",
    ".tar", ".tgz", ".wav", ".webm", ".woff", ".woff2", ".xls",
    ".xlsx", ".xml", ".zip",
)
_SESSION_KEYS = {
    "jsessionid", "phpsessid", "session", "session_id", "sessionid", "sid",
}
_PAGE_KEYS = {"page", "paged", "p", "page_number", "pagenumber"}
_OFFSET_KEYS = {"offset", "start", "from"}
_FACET_PREFIXES = ("facet", "filter", "refine")
_CALENDAR_SEGMENTS = {"archive", "archives", "calendar", "events"}
_DATE_PATH = re.compile(r"/(?:19|20)\d{2}/(?:0?[1-9]|1[0-2])(?:/|$)")


@dataclass(frozen=True)
class DiscoveredLink:
    url: str
    kind: Literal["page", "document", "image"]
    source: str


@dataclass(frozen=True)
class PageDiscoveryPolicy:
    base_url: str
    canonical_url: Optional[str]
    follow_links: bool
    index_page: bool


def _config_value(config: Any, names: Sequence[str], default: Any) -> Any:
    for name in names:
        if isinstance(config, Mapping) and name in config:
            return config[name]
        if hasattr(config, name):
            return getattr(config, name)
    return default


def _http_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return (
        parsed.scheme.lower() in ("http", "https")
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
    )


def _resolution_url(url: str) -> Optional[str]:
    """Normalize identity fields while retaining a meaningful directory slash."""
    if not _http_url(url):
        return None
    try:
        raw = urlsplit(url)
        normalized = normalize_url(url)
        if not normalized:
            return None
        parsed = urlsplit(normalized)
        path = parsed.path
        if raw.path.endswith("/") and not path.endswith("/"):
            path += "/"
        return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))
    except ValueError:
        return None


def _rel_tokens(element: Any) -> tuple[str, ...]:
    raw = element.get("rel") or ()
    if isinstance(raw, str):
        raw = raw.split()
    return tuple(str(token).lower() for token in raw)


def _meta_robot_directives(soup: BeautifulSoup) -> set[str]:
    directives: set[str] = set()
    for meta in soup.find_all("meta"):
        name = str(meta.get("name") or "").strip().lower()
        if name not in ("robots", "crawltrove"):
            continue
        content = str(meta.get("content") or "").lower()
        directives.update(token for token in re.split(r"[\s,]+", content) if token)
    if "none" in directives:
        directives.update(("noindex", "nofollow"))
    return directives


def page_discovery_policy(html: str, page_url: str) -> PageDiscoveryPolicy:
    """Extract base, canonical, and meta-robots policy from discovery HTML."""
    soup = BeautifulSoup(html or "", "html.parser")
    base_url = _resolution_url(page_url) or page_url
    base = soup.find("base", href=True)
    if base:
        candidate = _resolution_url(urljoin(page_url, str(base.get("href") or "")))
        if candidate:
            base_url = candidate

    canonical_url = None
    for link in soup.find_all("link", href=True):
        if "canonical" not in _rel_tokens(link):
            continue
        candidate = urljoin(base_url, str(link.get("href") or ""))
        if _http_url(candidate):
            canonical_url = normalize_url(candidate) or None
        break

    directives = _meta_robot_directives(soup)
    return PageDiscoveryPolicy(
        base_url=base_url,
        canonical_url=canonical_url,
        follow_links="nofollow" not in directives,
        index_page="noindex" not in directives,
    )


def crawl_trap_reason(url: str) -> Optional[str]:
    """Return a conservative static crawl-trap reason, if one is evident."""
    try:
        if len((url or "").encode("utf-8")) > MAX_URL_BYTES:
            return "url_too_long"
    except UnicodeError:
        return "invalid_url"
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "invalid_url"

    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) > 50:
        return "path_segments"
    if segments and max(Counter(segments).values()) > 5:
        return "repeated_path_segment"
    lowered_path = parsed.path.lower()
    if ";jsessionid=" in lowered_path:
        return "session_identifier"

    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    lowered_pairs = [(key.casefold(), value) for key, value in pairs]
    if any(key in _SESSION_KEYS for key, _ in lowered_pairs):
        return "session_identifier"

    for key, value in lowered_pairs:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if key in _PAGE_KEYS and number > 1000:
            return "pagination"
        if key in _OFFSET_KEYS and number > 100000:
            return "pagination"

    facet_keys = [
        key for key, _ in lowered_pairs
        if key.startswith(_FACET_PREFIXES) or key.startswith("f[")
    ]
    repeated_facets = bool(
        facet_keys and len(set(facet_keys)) < len(facet_keys) - 2
    )
    if len(facet_keys) >= 4 or repeated_facets:
        return "faceted_search"

    lowered_segments = {segment.casefold() for segment in segments}
    has_calendar_context = bool(lowered_segments & _CALENDAR_SEGMENTS)
    query_keys = {key for key, _ in lowered_pairs}
    if (
        has_calendar_context and _DATE_PATH.search(lowered_path)
    ) or {"year", "month", "day"}.issubset(query_keys):
        return "calendar"
    return None


def _link_kind(url: str) -> Literal["page", "document", "image"]:
    path = urlsplit(url).path.lower()
    if path.endswith(_DOCUMENT_SUFFIXES):
        return "document"
    if path.endswith(_IMAGE_SUFFIXES):
        return "image"
    return "page"


def _candidate(element: Any, config: CrawlConfig) -> tuple[Optional[str], str]:
    name = element.name.lower()
    rel = _rel_tokens(element)
    if "nofollow" in rel:
        return None, ""
    if name in ("a", "area"):
        return element.get("href"), "anchor" if name == "a" else "area"
    if name == "link":
        if "canonical" in rel:
            return None, ""
        source = next((value for value in ("next", "prev", "alternate")
                       if value in rel), None)
        return (element.get("href"), source or "") if source else (None, "")
    if name == "iframe":
        enabled = bool(_config_value(
            config, ("discoverIframes", "discover_iframes"), False
        ))
        return (element.get("src"), "iframe") if enabled else (None, "")
    if name in ("img", "source"):
        enabled = bool(_config_value(
            config, ("discoverEmbeddedImages", "discover_embedded_images"), False
        ))
        return (element.get("src"), "embedded_image") if enabled else (None, "")
    if name in ("object", "embed"):
        return element.get("data") or element.get("src"), "embedded_document"
    return None, ""


def discover_links(
    html: str,
    page_url: str,
    config: CrawlConfig,
    *,
    rejection_counts: Optional[MutableMapping[str, int]] = None,
) -> list[DiscoveredLink]:
    """Return same-origin links in document order with ordered deduplication."""
    policy = page_discovery_policy(html, page_url)
    if not policy.follow_links:
        return []

    scope_url = str(_config_value(config, ("url",), page_url) or page_url)
    scope_origin = origin_key(scope_url) or origin_key(page_url)
    if not scope_origin:
        return []

    include_documents = bool(_config_value(
        config, ("includeDocuments", "include_documents"), True
    ))
    include_images = bool(_config_value(
        config, ("includeImages", "include_images", "extractLinkedImages",
                 "extract_linked_images"), False
    ))
    tracking_parameters = _config_value(
        config, ("trackingParameters", "tracking_parameters"), ()
    ) or ()
    if isinstance(tracking_parameters, str):
        tracking_parameters = (tracking_parameters,)
    max_children = int(_config_value(
        config, ("maxChildrenPerPage", "max_children_per_page"), 1000
    ))
    max_children = max(0, min(max_children, 1000))
    if max_children == 0:
        return []

    soup = BeautifulSoup(html or "", "html.parser")
    seen: set[str] = set()
    output: list[DiscoveredLink] = []
    for element in soup.find_all(
        ("a", "area", "link", "iframe", "img", "source", "object", "embed")
    ):
        raw, source = _candidate(element, config)
        if not raw:
            continue
        try:
            resolved = urljoin(policy.base_url, str(raw).strip())
        except ValueError:
            continue
        if not _http_url(resolved):
            continue
        normalized = normalize_url(
            resolved, tracking_parameters=tracking_parameters
        )
        if not normalized or origin_key(normalized) != scope_origin:
            continue

        kind = _link_kind(normalized)
        if kind == "document" and not include_documents:
            continue
        if kind == "image" and not include_images:
            continue
        if kind == "page" and urlsplit(normalized).path.lower().endswith(
            _SKIPPED_SUFFIXES
        ):
            continue
        if source in ("embedded_document", "embedded_image") and kind == "page":
            continue

        rejection = crawl_trap_reason(normalized)
        if rejection:
            if rejection_counts is not None:
                rejection_counts[rejection] = (
                    rejection_counts.get(rejection, 0) + 1
                )
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        output.append(DiscoveredLink(normalized, kind, source))
        if len(output) >= max_children:
            break
    return output


__all__ = [
    "DiscoveredLink",
    "PageDiscoveryPolicy",
    "crawl_trap_reason",
    "discover_links",
    "page_discovery_policy",
]
