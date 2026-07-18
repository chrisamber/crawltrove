"""FastMCP stdio server backed by a running CrawlTrove HTTP service."""
from typing import Callable, Optional

from mcp.server.fastmcp import FastMCP

from crawltrove_mcp.client import CrawlTroveClient, CrawlTroveError


mcp = FastMCP("crawltrove")
_client = CrawlTroveClient()


def _safe(call: Callable[[], dict]) -> dict:
    try:
        return call()
    except CrawlTroveError as exc:
        return {"error": str(exc), "kind": exc.kind, "status": exc.status}


@mcp.tool()
def scrape(url: str, engine: str = "auto", only_main_content: bool = True,
           wait_for_ms: int = 1000) -> dict:
    """Scrape one URL into clean Markdown and metadata."""
    return _safe(lambda: _client.scrape(
        url, engine=engine, only_main_content=only_main_content,
        wait_for_ms=wait_for_ms))


@mcp.tool()
def search_web(query: str, limit: int = 8) -> dict:
    """Search the web through CrawlTrove's configured provider waterfall."""
    return _safe(lambda: _client.search_web(query, limit=limit))


@mcp.tool()
def search(query: str, kind: Optional[str] = None, k: int = 10,
           mode: str = "hybrid", namespace: Optional[str] = None,
           bucket: Optional[str] = None, tier: Optional[str] = None,
           framework: Optional[str] = None) -> dict:
    """Search indexed CrawlTrove output using hybrid, semantic, or keyword retrieval."""
    return _safe(lambda: _client.search(
        query, kind=kind, k=k, mode=mode, namespace=namespace, bucket=bucket,
        tier=tier, framework=framework))


@mcp.tool()
def start_crawl(url: str, limit: int = 10, max_depth: int = 3,
                use_sitemap: bool = True, engine: str = "auto") -> dict:
    """Start an asynchronous crawl and return its job identifier."""
    return _safe(lambda: _client.start_crawl(
        url, limit=limit, max_depth=max_depth,
        use_sitemap=use_sitemap, engine=engine))


@mcp.tool()
def get_crawl(job_id: str) -> dict:
    """Poll a crawl without returning full page bodies."""
    return _safe(lambda: _client.get_crawl(job_id))
