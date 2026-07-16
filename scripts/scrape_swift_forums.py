#!/usr/bin/env python3
"""Collect Swift Forums threads via the Discourse JSON API (Epic 3 S5).

forums.swift.org is a Discourse instance: every page has a `.json` twin. We list
a category's topics (`/c/<slug>/<id>.json`), fetch each topic
(`/t/<id>.json`), and render the thread to Markdown — original question first,
the accepted answer flagged. Accepted-answer threads are prioritized (they're
the most SFT-shaped); the S3 quality tiering does the noise control downstream.

Forum posts are user-contributed, so the source is routed conservatively
(community-review-required → RAG-only) in license_buckets.py.

Writes a crawl artifact to data/crawls/ (source=swift-forums).

Usage:
    python scripts/scrape_swift_forums.py --category evolution/18 --max-topics 50
    python scripts/scrape_swift_forums.py --category using-swift/6 --accepted-only
"""
from __future__ import annotations

import argparse
import sys

from bs4 import BeautifulSoup

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import collectors_common as cc

BASE_URL = "https://forums.swift.org"


def _html_to_text(html: str) -> str:
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text("\n").strip()


def thread_to_result(topic: dict, base_url: str = BASE_URL):
    """Render one Discourse topic JSON to a crawl result row, or None if empty.

    Pure: takes the parsed `/t/<id>.json` payload. The accepted answer is flagged
    from either the topic's `accepted_answer_post_id` or a post's
    `accepted_answer` flag."""
    tid = topic.get("id")
    slug = topic.get("slug") or ""
    url = f"{base_url}/t/{slug}/{tid}" if slug else f"{base_url}/t/{tid}"
    title = topic.get("title") or topic.get("fancy_title") or ""
    posts = ((topic.get("post_stream") or {}).get("posts")) or []
    if not posts:
        return None
    accepted_id = topic.get("accepted_answer_post_id")
    tags = topic.get("tags") or []

    parts = [f"# {title}"]
    if tags:
        parts.append("Tags: " + ", ".join(tags))
    has_accepted = False
    for p in posts:
        author = p.get("username") or p.get("name") or "user"
        body = _html_to_text(p.get("cooked") or p.get("raw") or "")
        if not body:
            continue
        is_accepted = bool(p.get("accepted_answer")) or (
            accepted_id is not None and p.get("id") == accepted_id)
        has_accepted = has_accepted or is_accepted
        marker = " (accepted answer)" if is_accepted else ""
        parts.append(f"## {author}{marker}\n\n{body}")

    markdown = "\n\n".join(parts).strip()
    if len(parts) <= 1:
        return None
    return cc.make_result(url, title, markdown, source_kind="forum-thread",
                          accepted=has_accepted, reply_count=max(0, len(posts) - 1),
                          tags=tags or None)


def select_topics(topic_list: list, *, max_topics: int, accepted_only: bool) -> list:
    """Pick topic ids from a category listing, accepted-answer threads first."""
    topics = list(topic_list or [])
    if accepted_only:
        topics = [t for t in topics if t.get("has_accepted_answer")]
    # Accepted answers first, then by reply/like activity — most useful on top.
    topics.sort(key=lambda t: (
        not t.get("has_accepted_answer"),
        -(t.get("reply_count") or 0),
        -(t.get("like_count") or 0)))
    return topics[:max_topics]


def collect(category: str, *, max_topics: int, accepted_only: bool,
            get_json=cc.get_json, base_url: str = BASE_URL) -> list:
    listing = get_json(f"{base_url}/c/{category}.json")
    topics = ((listing.get("topic_list") or {}).get("topics")) or []
    selected = select_topics(topics, max_topics=max_topics, accepted_only=accepted_only)
    results = []
    for t in selected:
        try:
            topic = get_json(f"{base_url}/t/{t['id']}.json")
        except Exception as e:
            print(f"  ! topic {t.get('id')} failed: {e}")
            continue
        row = thread_to_result(topic, base_url)
        if row is not None:
            results.append(row)
            print(f"  + {row['metadata']['title']}"
                  f"{' [accepted]' if row['metadata'].get('accepted') else ''}")
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--category", required=True,
                    help="Discourse category path, e.g. 'evolution/18' or 'using-swift/6'")
    ap.add_argument("--max-topics", type=int, default=50)
    ap.add_argument("--accepted-only", action="store_true",
                    help="only threads that have an accepted answer")
    ap.add_argument("--out", default="", help="explicit artifact path (default: auto-stem)")
    args = ap.parse_args()

    results = collect(args.category, max_topics=args.max_topics,
                      accepted_only=args.accepted_only)
    if not results:
        print("no threads collected")
        return 1
    path = cc.write_artifact(results, base_url=f"{BASE_URL}/c/{args.category}",
                             source="swift-forums", out=args.out)
    print(f"scrape_swift_forums: {len(results)} threads -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
