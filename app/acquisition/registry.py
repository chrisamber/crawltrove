"""Provider availability without account probes or secret-bearing health output."""
from __future__ import annotations

from dataclasses import dataclass
import os
import inspect
from typing import Iterable, Mapping

from app.acquisition.providers import ProviderAdapter


ROUTE_PROVIDER = {
    "local_http": "local",
    "owned_proxy_http": "local",
    "local_browser": "local",
    "brightdata_unlocker": "brightdata",
    "firecrawl_scrape": "firecrawl",
    "browserbase_session": "browserbase",
    "firecrawl_interact": "firecrawl",
}

PROVIDER_ROUTES = {
    "local": ("local_http", "owned_proxy_http", "local_browser"),
    "brightdata": ("brightdata_unlocker",),
    "firecrawl": ("firecrawl_scrape", "firecrawl_interact"),
    "browserbase": ("browserbase_session",),
}


class ProviderUnavailable(RuntimeError):
    """Stable unavailable-provider signal for callers and API integration."""

    code = "provider_unavailable"


@dataclass(frozen=True)
class ProviderHealth:
    state: str


class ProviderRegistry:
    """Tracks configured provider adapters without persisting credentials."""

    def __init__(self, adapters: Mapping[str, ProviderAdapter], *,
                 allowed_routes: Iterable[str] | None = None) -> None:
        if set(adapters) - set(PROVIDER_ROUTES):
            raise ValueError("unknown acquisition provider")
        self._adapters = dict(adapters)
        self._allowed_routes = (
            frozenset(allowed_routes) if allowed_routes is not None else None
        )
        self._health = {
            name: ProviderHealth("configured" if adapter.available() else "disabled")
            for name, adapter in self._adapters.items()
        }

    def disable(self, provider: str, reason: str = "disabled") -> None:
        del reason
        self._require_known(provider)
        self._health[provider] = ProviderHealth("disabled")

    def degrade(self, provider: str) -> None:
        self._require_known(provider)
        self._health[provider] = ProviderHealth("degraded")

    def unhealthy(self, provider: str) -> None:
        self._require_known(provider)
        self._health[provider] = ProviderHealth("unhealthy")

    def health(self) -> dict[str, dict[str, str]]:
        return {name: {"state": health.state} for name, health in self._health.items()}

    def adapter_for_route(self, route: str) -> ProviderAdapter:
        provider = ROUTE_PROVIDER.get(route)
        if provider is None or provider not in self._adapters:
            raise ProviderUnavailable("provider_unavailable")
        return self._adapters[provider]

    def route_available(self, route: str) -> bool:
        if self._allowed_routes is not None and route not in self._allowed_routes:
            return False
        provider = ROUTE_PROVIDER.get(route)
        if provider is None or self._health.get(provider) != ProviderHealth("configured"):
            return False
        adapter = self._adapters.get(provider)
        if adapter is None or not adapter.available():
            return False
        route_available = getattr(adapter, "route_available", None)
        return bool(route_available(route)) if route_available is not None else route in adapter.routes

    def available_routes(self, provider: str) -> list[str]:
        if provider == "auto":
            return [route for routes in PROVIDER_ROUTES.values() for route in routes
                    if self.route_available(route)]
        self._require_known(provider)
        if self._health.get(provider) != ProviderHealth("configured"):
            raise ProviderUnavailable("provider_unavailable")
        return [route for route in PROVIDER_ROUTES[provider] if self.route_available(route)]

    def set_proxy_pool(self, proxy_pool: object | None) -> None:
        """Bind the local adapter after durable proxy state is initialized."""
        adapter = self._adapters.get("local")
        set_proxy_pool = getattr(adapter, "set_proxy_pool", None)
        if set_proxy_pool is not None:
            set_proxy_pool(proxy_pool)

    def require_available(self, provider: str) -> None:
        self.available_routes(provider)

    def _require_known(self, provider: str) -> None:
        if provider not in PROVIDER_ROUTES or provider not in self._adapters:
            raise ProviderUnavailable("provider_unavailable")

    async def aclose(self) -> None:
        """Close provider clients without exposing credentials or sessions."""
        for adapter in self._adapters.values():
            close = getattr(adapter, "aclose", None) or getattr(adapter, "close", None)
            if close is None:
                continue
            result = close()
            if inspect.isawaitable(result):
                await result


def env_registry(scraper, *, proxy_pool=None,
                 allowed_routes: Iterable[str] | None = None) -> ProviderRegistry:
    """Build once from env; availability is observable but secrets never are."""
    from app.acquisition.brightdata import BrightDataAdapter
    from app.acquisition.browserbase import BrowserbaseAdapter
    from app.acquisition.firecrawl import FirecrawlAdapter
    from app.acquisition.local import LocalAdapter

    return ProviderRegistry({
        "local": LocalAdapter(scraper, proxy_pool),
        "firecrawl": FirecrawlAdapter(os.getenv("FIRECRAWL_API_KEY")),
        "brightdata": BrightDataAdapter(
            os.getenv("BRIGHTDATA_API_KEY"), os.getenv("BRIGHTDATA_ZONE"),
        ),
        "browserbase": BrowserbaseAdapter(
            os.getenv("BROWSERBASE_API_KEY", ""), os.getenv("BROWSERBASE_PROJECT_ID", ""),
        ),
    }, allowed_routes=allowed_routes)
