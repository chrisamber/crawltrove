"""Local durable-acquisition routes around the existing scraper."""
from __future__ import annotations

from typing import Any, Mapping

from app.acquisition.providers import NativeCost, ProviderFailure, ProviderRequest, ProviderResult


class LocalAdapter:
    """Expose existing local HTTP/browser work through the provider contract."""

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
        engine = "browser" if request.route == "local_browser" else "http"
        return await self._scrape(request, engine=engine)

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
            return await self._scrape(request, engine="http", proxy=lease.playwright_proxy())
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

    async def _scrape(
        self, request: ProviderRequest, *, engine: str, proxy: Mapping[str, str] | None = None,
    ) -> ProviderResult:
        result = await self.scraper.scrape(
            request.url, only_main_content=request.only_main_content, engine=engine,
            proxy=dict(proxy) if proxy is not None else None,
            trust_env=False,
            max_decoded_bytes=request.max_decoded_bytes,
        )
        if not isinstance(result, Mapping):
            raise ProviderFailure("provider_protocol_error", False, NativeCost({}))
        metadata = result.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        status = metadata.get("status_code")
        status = status if isinstance(status, int) else None
        if result.get("success") is not True:
            reason = metadata.get("reason")
            code = reason if isinstance(reason, str) and reason else "local_failure"
            retryable = code in {"blocked_challenge", "transport_error", "timeout"}
            retryable = retryable or status == 429 or (status is not None and status >= 500)
            raise ProviderFailure(code, retryable, NativeCost({}), status)
        html = result.get("discovery_html") or result.get("html")
        final_url = result.get("url") or request.url
        if not isinstance(html, str) or not isinstance(final_url, str):
            raise ProviderFailure("provider_protocol_error", False, NativeCost({}), status)
        return ProviderResult(html, final_url, status, NativeCost({}))

    async def cancel(self, remote_id: str) -> None:
        return None
