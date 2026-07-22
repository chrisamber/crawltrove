"""Isolated Crawl4AI invocation with its cache explicitly bypassed."""
from __future__ import annotations

import asyncio
import json
import sys
import time


async def run(url: str) -> dict[str, object]:
    from crawl4ai import AsyncWebCrawler, CacheMode

    started = time.perf_counter()
    async with AsyncWebCrawler() as crawler:
        result = await crawler.arun(url=url, cache_mode=CacheMode.BYPASS)
    markdown = getattr(result, "markdown", "") or ""
    return {
        "success": bool(getattr(result, "success", False)),
        "markdown": str(markdown),
        "durationSeconds": time.perf_counter() - started,
        "outputBytes": len(str(markdown).encode("utf-8")),
    }


def main() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read())
        if not isinstance(payload, dict) or not isinstance(payload.get("url"), str):
            return 2
        result = asyncio.run(run(payload["url"]))
        sys.stdout.write(json.dumps(result, separators=(",", ":")))
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
