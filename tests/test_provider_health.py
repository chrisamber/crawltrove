import pytest


class _Adapter:
    def __init__(self, available):
        self._available = available
        self.routes = frozenset({"firecrawl_scrape", "firecrawl_interact"})

    def available(self):
        return self._available

    def reserve_cost(self, request):
        raise AssertionError("health checks must not reserve usage")

    async def acquire(self, request):
        raise AssertionError("health checks must not make provider calls")

    async def cancel(self, remote_id):
        return None


def test_auto_skips_missing_provider_but_explicit_fails():
    from app.acquisition.registry import ProviderRegistry, ProviderUnavailable

    registry = ProviderRegistry({"firecrawl": _Adapter(False)})
    assert "firecrawl_scrape" not in registry.available_routes("auto")
    with pytest.raises(ProviderUnavailable) as error:
        registry.available_routes("firecrawl")
    assert error.value.code == "provider_unavailable"
    assert registry.health() == {"firecrawl": {"state": "disabled"}}


def test_health_never_exposes_disable_reason():
    from app.acquisition.registry import ProviderRegistry

    registry = ProviderRegistry({"firecrawl": _Adapter(True)})
    registry.disable("firecrawl", "Bearer secret")
    assert registry.health() == {"firecrawl": {"state": "disabled"}}


def test_health_supports_degraded_and_unhealthy_states():
    from app.acquisition.registry import ProviderRegistry

    registry = ProviderRegistry({"firecrawl": _Adapter(True)})
    registry.degrade("firecrawl")
    assert registry.health() == {"firecrawl": {"state": "degraded"}}
    registry.unhealthy("firecrawl")
    assert registry.health() == {"firecrawl": {"state": "unhealthy"}}
