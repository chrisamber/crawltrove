"""Tests for the corpus collectors and their license routing.

Hermetic: network fetch is injected as a fake; only the pure parsing/selection/
routing logic is exercised. The collectors live in scripts/, imported as modules.
"""
import importlib

import pytest

from app.corpus import license_buckets as lb
from app.corpus import router

forums = importlib.import_module("scripts.scrape_swift_forums")
swiftorg = importlib.import_module("scripts.scrape_swiftorg")
tutorials = importlib.import_module("scripts.scrape_tutorials")
ghdocs = importlib.import_module("scripts.scrape_github_docs")


# --- license buckets + routing -------------------------------------------
def test_new_buckets_are_known_and_routed():
    assert "permissive" in lb.ALL_BUCKETS
    assert "community-review-required" in lb.ALL_BUCKETS
    assert router.route({"license_bucket": "permissive"}) == {"rag", "dapt"}
    assert router.route({"license_bucket": "community-review-required"}) == {"rag"}


def test_every_mapped_bucket_is_known():
    for bucket in lb.SOURCE_BUCKET.values():
        assert bucket in lb.ALL_BUCKETS


def test_source_id_for_repo_license():
    assert lb.source_id_for_repo_license("MIT") == "github-permissive"
    assert lb.source_id_for_repo_license("Apache-2.0") == "github-permissive"
    assert lb.source_id_for_repo_license("GPL-3.0") == "github-docs"
    assert lb.source_id_for_repo_license(None) == "github-docs"
    # and each resolves to the intended bucket
    assert lb.bucket_for("github-permissive") == "permissive"
    assert lb.bucket_for("github-docs") == "community-review-required"


def test_source_id_for_detected_license():
    assert lb.source_id_for_detected_license("CC-BY-4.0") == "tutorials-cc-by"
    assert lb.source_id_for_detected_license("CC0-1.0") == "tutorials-cc0"
    assert lb.source_id_for_detected_license(None) == "tutorials"
    assert lb.bucket_for("tutorials-cc-by") == "cc-by-4.0"
    assert lb.bucket_for("tutorials") == "community-review-required"


def test_swiftorg_and_forums_buckets():
    assert lb.bucket_for("swiftorg") == "swift-org-permissive"
    assert lb.bucket_for("swift-forums") == "community-review-required"


# --- Swift Forums (Discourse) ---------------------------------------------
def _topic(tid=1, accepted_id=None, posts=None, tags=None):
    return {"id": tid, "slug": "how-to-actor", "title": "How to actor?",
            "tags": tags or [], "accepted_answer_post_id": accepted_id,
            "post_stream": {"posts": posts or []}}


def test_thread_to_result_flags_accepted_answer():
    topic = _topic(accepted_id=22, posts=[
        {"id": 21, "username": "asker", "cooked": "<p>How do actors work?</p>"},
        {"id": 22, "username": "expert", "cooked": "<p>They isolate state.</p>"},
    ])
    row = forums.thread_to_result(topic)
    assert row["url"].endswith("/t/how-to-actor/1")
    assert "How do actors work?" in row["markdown"]
    assert "(accepted answer)" in row["markdown"]
    assert row["metadata"]["accepted"] is True
    assert row["metadata"]["reply_count"] == 1


def test_thread_to_result_empty_is_none():
    assert forums.thread_to_result(_topic(posts=[])) is None


def test_select_topics_prioritizes_accepted():
    topics = [
        {"id": 1, "has_accepted_answer": False, "reply_count": 9},
        {"id": 2, "has_accepted_answer": True, "reply_count": 1},
    ]
    picked = forums.select_topics(topics, max_topics=5, accepted_only=False)
    assert [t["id"] for t in picked] == [2, 1]
    only = forums.select_topics(topics, max_topics=5, accepted_only=True)
    assert [t["id"] for t in only] == [2]


def test_forums_collect_with_fake_fetch():
    calls = {"c": {"topic_list": {"topics": [{"id": 7, "has_accepted_answer": True}]}},
             "t": _topic(tid=7, accepted_id=71, posts=[
                 {"id": 70, "username": "a", "cooked": "<p>Q</p>"},
                 {"id": 71, "username": "b", "cooked": "<p>A</p>"}])}

    def fake_get_json(url):
        return calls["c"] if "/c/" in url else calls["t"]

    rows = forums.collect("evolution/18", max_topics=5, accepted_only=False,
                          get_json=fake_get_json)
    assert len(rows) == 1 and rows[0]["metadata"]["accepted"] is True


# --- swift.org URL filtering ----------------------------------------------
def test_is_content_url():
    assert swiftorg.is_content_url("https://www.swift.org/blog/swift-6/")
    assert not swiftorg.is_content_url("https://www.swift.org/assets/logo.png")
    assert not swiftorg.is_content_url("https://example.com/blog/")
    assert not swiftorg.is_content_url("https://www.swift.org/sitemap.xml")


def test_select_urls_dedups_and_caps():
    urls = ["https://www.swift.org/blog/a/", "https://www.swift.org/blog/a#x",
            "https://www.swift.org/blog/b", "https://other.com/c",
            "https://www.swift.org/logo.png"]
    got = swiftorg.select_urls(urls, max_pages=10)
    assert got == ["https://www.swift.org/blog/a", "https://www.swift.org/blog/b"]


# --- tutorials license gate -----------------------------------------------
def test_page_passes_license_gate():
    assert tutorials.page_passes("CC-BY-4.0", "CC-BY-4.0")
    assert tutorials.page_passes("CC-BY-4.0", "CC-BY-SA-4.0")   # same family
    assert tutorials.page_passes("CC-BY-4.0", None)             # trust allowlist
    assert not tutorials.page_passes("CC-BY-4.0", "GPL-3.0")    # conflict -> drop


# --- GitHub docs selection -------------------------------------------------
def test_select_doc_paths():
    tree = [
        {"path": "README.md", "type": "blob"},
        {"path": "docs/guide.md", "type": "blob"},
        {"path": "docs/adv/deep.markdown", "type": "blob"},
        {"path": "Sources/x.swift", "type": "blob"},
        {"path": "docs", "type": "tree"},
    ]
    paths = ghdocs.select_doc_paths(tree)
    assert paths[0] == "README.md"
    assert "docs/guide.md" in paths
    assert "docs/adv/deep.markdown" in paths
    assert "Sources/x.swift" not in paths


def test_collect_repo_uses_license_for_source_id():
    def fake_get_json(url, headers=None):
        if url.endswith("/license"):
            return {"license": {"spdx_id": "Apache-2.0"}}
        if url.endswith("swift-nio"):
            return {"default_branch": "main"}
        if "git/trees" in url:
            return {"tree": [{"path": "README.md", "type": "blob"}]}
        return {}

    def fake_get_text(url, headers=None):
        return "# swift-nio\n\nNon-blocking networking."

    source_id, results = ghdocs.collect_repo("apple/swift-nio",
                                             get_json=fake_get_json,
                                             get_text=fake_get_text)
    assert source_id == "github-permissive"
    assert len(results) == 1
    assert results[0]["metadata"]["repo_license"] == "Apache-2.0"
    assert lb.bucket_for(source_id) == "permissive"
