"""Local durable-acquisition routes: raw HTTP/browser capture only.

Normalization (markdown, signals, license) is owned by AcquisitionRouter so
local pages are not scrape → strip → rebuild.
"""
from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import urlsplit

from app import documents, fetch
from app.acquisition.providers import NativeCost, ProviderFailure, ProviderRequest, ProviderResult
from app.url_safety import UnsafeUrlError, ensure_public_url


def _same_origin(left: str, right: str) -> bool:
    a, b = urlsplit(left), urlsplit(right)
    return (a.scheme, a.hostname, a.port or (443 if a.scheme == "https" else 80)) == (
        b.scheme, b.hostname, b.port or (443 if b.scheme == "https" else 80),
    )


class LocalAdapter:
    """Raw local HTTP/browser capture through the provider contract."""

    name = "local"
    routes = frozenset({"local_http", "owned_proxy_http", "local_browser"})

    def __init__(self, scraper: Any, proxy_pool: Any | None = None) -> None:
        self.scraper = scraper
        self._proxy_pool = proxy_pool

    def set_proxy_pool(self, proxy_pool: Any | None) -> None:
        """Install the initialized pool without rebuilding provider clients."""
        self._proxy_pool = proxy_pool

    def available(self) -> bool:
        return True

    def route_available(self, route: str) -> bool:
        return route != "owned_proxy_http" or self._proxy_pool is not None

    def reserve_cost(self, request: ProviderRequest) -> NativeCost:
        if request.route not in self.routes:
            raise ValueError("unknown local acquisition route")
        return NativeCost({})

    async def acquire(self, request: ProviderRequest) -> ProviderResult:
        if request.route == "owned_proxy_http":
            raise ValueError("owned proxy acquisition requires task lease context")
        if request.route == "local_browser":
            return await self._acquire_browser(request, proxy=request.proxy)
        return await self._acquire_http(request, proxy=request.proxy)

    async def acquire_owned_proxy(
        self, request: ProviderRequest, *, origin_key: str, task_id: object, lease_token: object,
    ) -> ProviderResult:
        if self._proxy_pool is None:
            raise ProviderFailure("provider_unavailable", True, NativeCost({}))
        lease = await self._proxy_pool.select(
            origin_key, task_id=task_id, lease_token=lease_token,
        )
        if lease is None:
            raise ProviderFailure("provider_unavailable", True, NativeCost({}))
        try:
            return await self._acquire_http(request, proxy=lease.playwright_proxy())
        except ProviderFailure as exc:
            if exc.code == "blocked_challenge":
                await self._proxy_pool.mark_failure(
                    lease.node_id, "blocked", task_id=task_id, lease_token=lease_token,
                )
            elif exc.retryable:
                await self._proxy_pool.mark_failure(
                    lease.node_id, "transport", task_id=task_id, lease_token=lease_token,
                )
            raise
        finally:
            await self._proxy_pool.release_proxy(task_id, lease_token)

    async def _acquire_http(
        self, request: ProviderRequest, *, proxy: Mapping[str, str] | None = None,
    ) -> ProviderResult:
        fetch_options: dict[str, Any] = {}
        if proxy is not None:
            server = proxy.get("server")
            if not isinstance(server, str) or not server:
                raise ProviderFailure("provider_request", False, NativeCost({}))
            fetch_options["proxy"] = server
            if "username" in proxy and "password" in proxy:
                fetch_options["proxy_auth"] = (proxy["username"], proxy["password"])
        kwargs: dict[str, Any] = dict(fetch_options)
        if request.max_decoded_bytes is not None:
            kwargs["max_decoded_bytes"] = request.max_decoded_bytes
        resp = await fetch.fetch_http(request.url, **kwargs)
        if resp is None:
            raise ProviderFailure("transport_error", True, NativeCost({}))
        status = resp.get("status")
        status = status if isinstance(status, int) else None
        final_url = resp.get("final_url") or request.url
        if not isinstance(final_url, str):
            raise ProviderFailure("provider_protocol_error", False, NativeCost({}), status)
        if not _same_origin(request.url, final_url):
            raise ProviderFailure("policy_error", False, NativeCost({}), status)
        if status is None or not 200 <= status < 300:
            code = "http_status_error"
            retryable = status == 429 or (status is not None and status >= 500)
            raise ProviderFailure(code, retryable, NativeCost({}), status)
        content = resp.get("content") or b""
        downloaded = len(content) if isinstance(content, (bytes, bytearray)) else 0
        kind = documents.sniff(resp.get("content_type", "") or "", final_url)
        if kind:
            return await self._document_result(resp, final_url, kind, status, downloaded)
        html = resp.get("html")
        if not isinstance(html, str):
            raise ProviderFailure("provider_protocol_error", False, NativeCost({}), status)
        if fetch.is_challenge_html(html):
            raise ProviderFailure(
                "blocked_challenge", True, NativeCost({}), status,
            )
        return ProviderResult(
            html, final_url, status, NativeCost({}),
            downloaded_bytes=downloaded,
        )

    async def _document_result(
        self, resp: Mapping[str, Any], final_url: str, kind: str,
        status: int | None, downloaded: int,
    ) -> ProviderResult:
        build_doc = getattr(self.scraper, "_build_document_result", None)
        if build_doc is None:
            raise ProviderFailure("local_failure", False, NativeCost({}), status)
        doc = await build_doc(resp, final_url, kind)
        if not doc:
            html = resp.get("html") if isinstance(resp.get("html"), str) else ""
            return ProviderResult(
                html or "", final_url, status, NativeCost({}),
                downloaded_bytes=downloaded,
            )
        meta = doc.get("metadata") if isinstance(doc.get("metadata"), Mapping) else {}
        return ProviderResult(
            raw_html="",
            final_url=final_url,
            status_code=status,
            native_cost=NativeCost({}),
            downloaded_bytes=downloaded,
            prebuilt_title=str(doc.get("title") or ""),
            prebuilt_markdown=str(doc.get("markdown") or ""),
            prebuilt_metadata=dict(meta),
        )

    async def _acquire_browser(
        self, request: ProviderRequest, *, proxy: Mapping[str, str] | None = None,
    ) -> ProviderResult:
        try:
            await ensure_public_url(request.url)
        except UnsafeUrlError as exc:
            raise ProviderFailure("unsafe_request_url", False, NativeCost({})) from exc
        before = request.before_browser
        if before is not None:
            allowed = await before()
            if not allowed:
                raise ProviderFailure(
                    "browser_budget_exhausted", False, NativeCost({}),
                )
        browser = getattr(self.scraper, "browser", None)
        if browser is None or not hasattr(browser, "render"):
            raise ProviderFailure("local_failure", False, NativeCost({}))
        max_bytes = request.max_decoded_bytes
        try:
            rendered = await browser.render(
                request.url,
                wait_for_ms=1000,
                capture_screenshot=bool(request.capture_screenshot),
                max_decoded_bytes=max_bytes if max_bytes is not None else 10 * 1024 * 1024,
                proxy=dict(proxy) if proxy is not None else None,
            )
        except UnsafeUrlError as exc:
            raise ProviderFailure("unsafe_request_url", False, NativeCost({})) from exc
        except Exception as exc:
            raise ProviderFailure(
                "transport_error", True, NativeCost({}),
            ) from exc
        html = rendered.get("html")
        final_url = rendered.get("final_url") or request.url
        status = rendered.get("status_code")
        status = status if isinstance(status, int) else None
        if not isinstance(html, str) or not isinstance(final_url, str):
            raise ProviderFailure("provider_protocol_error", False, NativeCost({}), status)
        if not _same_origin(request.url, final_url):
            raise ProviderFailure("policy_error", False, NativeCost({}), status)
        if rendered.get("blocked_challenge") or fetch.is_challenge_html(html):
            raise ProviderFailure("blocked_challenge", True, NativeCost({}), status)
        if status is None or not 200 <= status < 300:
            retryable = status == 429 or (status is not None and status >= 500)
            raise ProviderFailure(
                "http_status_error", retryable, NativeCost({}), status,
            )
        screenshot = rendered.get("screenshot")
        screenshot = screenshot if isinstance(screenshot, (bytes, bytearray)) else None
        return ProviderResult(
            html, final_url, status, NativeCost({}),
            downloaded_bytes=len(html.encode("utf-8")),
            screenshot=bytes(screenshot) if screenshot else None,
        )

    async def cancel(self, remote_id: str) -> None:
        return None
