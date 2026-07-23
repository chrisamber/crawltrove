from datetime import datetime, timedelta, timezone

from app import sitemap
from app.crawl.config import CrawlConfig
from app.crawl.discovery import (
    crawl_trap_reason,
    discover_links,
    page_discovery_policy,
)
from app.crawl.policy import (
    classify_robots_response,
    parse_retry_after,
    robots_decision,
    robots_outcome,
)
from app.normalize import normalize_url, origin_key


def crawl_config(**overrides):
    values = {"url": "https://example.com/start"}
    values.update(overrides)
    return CrawlConfig(**values)


def test_url_identity_normalizes_host_path_port_and_percent_encoding():
    assert normalize_url(
        "HTTPS://B\u00dcCHER.Example:443/a/./b/../%7euser/#part"
    ) == "https://xn--bcher-kva.example/a/~user"
    assert origin_key("HTTPS://B\u00dcCHER.Example/path") == (
        "https://xn--bcher-kva.example:443"
    )
    assert origin_key("http://example.com:8080/path") == (
        "http://example.com:8080"
    )


def test_url_identity_preserves_meaningful_query_order_and_duplicates():
    url = "https://example.com/search?b=2&a=1&a=3&b=4"
    assert normalize_url(url) == url
    assert normalize_url(
        "https://example.com/search?utm_source=x&b=2&a=1&utm_campaign=y",
        tracking_parameters={"utm_source", "utm_campaign"},
    ) == "https://example.com/search?b=2&a=1"


def test_url_identity_is_total_and_rejects_oversized_urls():
    assert normalize_url(None) == ""
    assert normalize_url("not a url") == "not a url"
    assert normalize_url("https://example.com/" + "a" * 4096) == ""


def test_discovery_is_document_ordered_and_keeps_documents():
    html = """
      <html><head><base href="https://example.com/docs/"></head><body>
      <a href="b">B</a><a href="/a#part">A</a><a href="b">B again</a>
      <a href="guide.pdf">PDF</a></body></html>
    """
    links = discover_links(html, "https://example.com/start", crawl_config())
    assert [link.url for link in links] == [
        "https://example.com/docs/b",
        "https://example.com/a",
        "https://example.com/docs/guide.pdf",
    ]
    assert [link.kind for link in links] == ["page", "page", "document"]


def test_discovery_applies_scope_nofollow_and_media_defaults():
    html = """
      <a href="https://other.example/page">outside</a>
      <a href="/private" rel="nofollow">private</a>
      <a href="/book.epub">book</a>
      <a href="/photo.png">photo</a>
      <link rel="next" href="/page-2">
    """
    links = discover_links(html, "https://example.com/start", crawl_config())
    assert [(link.url, link.source) for link in links] == [
        ("https://example.com/book.epub", "anchor"),
        ("https://example.com/page-2", "next"),
    ]


def test_page_policy_records_canonical_and_meta_robots_stops_following():
    html = """
      <head>
        <base href="https://example.com/docs/">
        <link rel="canonical" href="guide">
        <meta name="robots" content="index, nofollow">
      </head>
      <body><a href="next">next</a></body>
    """
    policy = page_discovery_policy(html, "https://example.com/start")
    assert policy.base_url == "https://example.com/docs/"
    assert policy.canonical_url == "https://example.com/docs/guide"
    assert policy.follow_links is False
    assert discover_links(html, "https://example.com/start", crawl_config()) == []


def test_trap_rejections_are_counted_without_links():
    html = """
      <a href="/ok">ok</a>
      <a href="/account?sessionid=secret">session</a>
      <a href="/archive?page=1000001">pagination</a>
      <a href="/calendar/2026/07/23">calendar</a>
    """
    rejected = {}
    links = discover_links(
        html, "https://example.com/start", crawl_config(),
        rejection_counts=rejected,
    )
    assert [link.url for link in links] == ["https://example.com/ok"]
    assert rejected == {
        "session_identifier": 1,
        "pagination": 1,
        "calendar": 1,
    }
    assert crawl_trap_reason("https://example.com/a/a/a/a/a/a") == (
        "repeated_path_segment"
    )


def test_robots_parser_uses_product_group_and_longest_matching_rule():
    body = """
      User-agent: *
      Disallow: /private/
      User-agent: CrawlTrove
      Disallow: /private/
      Allow: /private/public/
    """
    assert robots_decision(
        body, "CrawlTrove", "https://example.com/private/public/page"
    ) is True
    assert robots_decision(
        body, "CrawlTrove", "https://example.com/private/secret"
    ) is False


def test_seed_robots_denial_is_stable_failure():
    seed = robots_outcome(allowed=False, is_seed=True)
    child = robots_outcome(allowed=False, is_seed=False)
    assert seed.code == "seed_blocked_by_robots"
    assert seed.state == "permanent_failed"
    assert child.state == "blocked_robots"
    assert child.code == "blocked_robots"


def test_robots_response_policy_is_fail_closed_until_resolved():
    now = datetime(2026, 7, 23, tzinfo=timezone.utc)
    assert classify_robots_response(200, now=now).action == "parse"
    assert classify_robots_response(304, now=now).action == "refresh"
    assert classify_robots_response(403, now=now).action == "deny"
    assert classify_robots_response(404, now=now).action == "allow"
    deferred = classify_robots_response(429, retry_after="30", now=now)
    assert deferred.action == "defer"
    assert deferred.retry_at == now + timedelta(seconds=30)
    assert classify_robots_response(503, now=now).action == "retry"
    assert classify_robots_response(None, now=now).action == "retry"


def test_retry_after_supports_seconds_and_http_date():
    now = datetime(2026, 7, 23, tzinfo=timezone.utc)
    assert parse_retry_after("30", now) == now + timedelta(seconds=30)
    assert parse_retry_after("Thu, 23 Jul 2026 01:00:00 GMT", now) == datetime(
        2026, 7, 23, 1, 0, tzinfo=timezone.utc
    )
    assert parse_retry_after("-1", now) is None
    assert parse_retry_after("not-a-delay", now) is None


async def test_sitemap_discovery_resolves_relative_entries_in_order(monkeypatch):
    bodies = {
        "https://example.com/robots.txt": (
            "Sitemap: /sitemap.xml\nSitemap: https://other.example/offsite.xml"
        ),
        "https://example.com/sitemap.xml": """
          <urlset>
            <url><loc>/b</loc></url>
            <url><loc>https://example.com/a#part</loc></url>
            <url><loc>/b</loc></url>
            <url><loc>https://other.example/x</loc></url>
          </urlset>
        """,
    }

    async def fake_get(url):
        return bodies.get(url)

    monkeypatch.setattr(sitemap, "_get", fake_get)
    assert await sitemap.discover("https://example.com/start") == [
        "https://example.com/b",
        "https://example.com/a",
    ]
