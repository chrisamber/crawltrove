"""Decide which knowledge namespace a record belongs to.

Keeps "language Swift" separate from "Apple SDK Swift" so a model/retriever
never conflates language rules with framework conventions.
"""
from __future__ import annotations

from urllib.parse import urlsplit


def namespace_for(url: str, source: str) -> str:
    if source in ("swift-book", "swift-evolution") or "swift-evolution" in (url or ""):
        return "swift-language"

    parts = urlsplit(url or "")
    host = parts.netloc.lower()
    path = parts.path.lower().strip("/")

    if "docs.swift.org" in host or "swift.org" in host:
        return "swift-language"

    if "developer.apple.com" in host:
        segs = path.split("/")
        # /documentation/<top>/...
        if len(segs) >= 2 and segs[0] == "documentation":
            top = segs[1]
            if top == "swift":
                return "swift-stdlib"
            if top == "xcode":
                return "xcode-tooling"
            return "apple-framework"
        if segs and segs[0] == "xcode":
            return "xcode-tooling"
        return "apple-framework"

    return "unknown"
