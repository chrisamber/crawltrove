"""Small synchronous client for CrawlTrove's MCP tool surface."""
import os
from typing import Any, Optional

import httpx


class CrawlTroveError(Exception):
    def __init__(self, message: str, *, kind: str, status: Optional[int] = None):
        super().__init__(message)
        self.kind = kind
        self.status = status


class CrawlTroveClient:
    def __init__(self, *, base_url: Optional[str] = None,
                 timeout: Optional[float] = None,
                 transport: Optional[httpx.BaseTransport] = None):
        self.base_url = (base_url or os.environ.get(
            "CRAWLTROVE_BASE_URL", "http://localhost:8000")).rstrip("/")
        self.timeout = float(timeout if timeout is not None else os.environ.get(
            "CRAWLTROVE_TIMEOUT", "120"))
        user = os.environ.get("CRAWLTROVE_USER")
        password = os.environ.get("CRAWLTROVE_PASSWORD")
        self.auth = (user, password) if user and password else None
        api_key = os.environ.get("CRAWLTROVE_API_KEY")
        self.headers = {"X-API-Key": api_key} if api_key else None
        self.transport = transport

    def _request(self, method: str, path: str, *, json: Any = None,
                 params: Optional[dict] = None) -> dict:
        kwargs = {
            "base_url": self.base_url,
            "timeout": self.timeout,
            "auth": self.auth,
            "headers": self.headers,
        }
        if self.transport is not None:
            kwargs["transport"] = self.transport
        try:
            with httpx.Client(**kwargs) as client:
                response = client.request(method, path, json=json, params=params)
        except httpx.TimeoutException as exc:
            raise CrawlTroveError(
                f"CrawlTrove request timed out after {self.timeout}s",
                kind="timeout") from exc
        except httpx.RequestError as exc:
            raise CrawlTroveError(
                f"Cannot reach CrawlTrove at {self.base_url}",
                kind="connection") from exc
        if response.status_code >= 400:
            try:
                error_body = response.json()
            except ValueError:
                error_body = None
            detail = (error_body.get("detail", response.text)
                      if isinstance(error_body, dict) else response.text)
            kind = {
                400: "bad_request", 401: "auth_required", 404: "not_found",
                422: "validation", 501: "not_configured",
                502: "upstream_failed",
            }.get(response.status_code, "http_error")
            raise CrawlTroveError(
                f"CrawlTrove returned {response.status_code}: {detail}",
                kind=kind, status=response.status_code)
        try:
            body = response.json()
        except ValueError as exc:
            raise CrawlTroveError(
                "CrawlTrove returned an invalid JSON response",
                kind="invalid_response", status=response.status_code) from exc
        if not isinstance(body, dict):
            raise CrawlTroveError(
                "CrawlTrove returned a non-object JSON response",
                kind="invalid_response", status=response.status_code)
        return body

    def scrape(self, url: str, *, engine: str = "auto",
               only_main_content: bool = True, wait_for_ms: int = 1000) -> dict:
        return self._request("POST", "/api/scrape", json={
            "url": url, "engine": engine,
            "onlyMainContent": only_main_content, "waitForMs": wait_for_ms,
        })

    def search_web(self, query: str, *, limit: int = 8) -> dict:
        return self._request("POST", "/api/search/web", json={
            "query": query, "limit": limit,
        })

    def search(self, query: str, *, kind: Optional[str] = None, k: int = 10,
               mode: str = "hybrid", namespace: Optional[str] = None,
               bucket: Optional[str] = None, tier: Optional[str] = None,
               framework: Optional[str] = None) -> dict:
        params = {"q": query, "k": k, "mode": mode}
        params.update({
            name: value for name, value in {
                "kind": kind, "namespace": namespace, "bucket": bucket,
                "tier": tier, "framework": framework,
            }.items() if value is not None
        })
        return self._request("GET", "/api/search/hybrid", params=params)

    def start_crawl(self, url: str, *, limit: int = 10, max_depth: int = 3,
                    use_sitemap: bool = True, engine: str = "auto") -> dict:
        return self._request("POST", "/api/crawl", json={
            "url": url, "limit": limit, "maxDepth": max_depth,
            "useSitemap": use_sitemap, "engine": engine,
        })

    def get_crawl(self, job_id: str) -> dict:
        job = self._request("GET", f"/api/crawl/{job_id}")
        results = job.get("results") or []
        errors = job.get("errors") or []
        return {
            "job_id": job.get("job_id", job_id),
            "status": job.get("status"),
            "progress": job.get("progress"),
            "processed_urls_count": job.get("processed_urls_count"),
            "limit": job.get("limit"),
            "pages": len(results),
            "errors": len(errors),
            "result_urls": [row.get("url") for row in results],
            "error_urls": [row.get("url") for row in errors],
        }
