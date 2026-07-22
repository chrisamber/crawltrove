"""Normalized managed-acquisition provider contracts and native meter rules."""
from dataclasses import dataclass
from typing import Mapping, Protocol


# Routes are persisted verbatim in the attempt ledger.  Native costs must use
# exactly these meter keys so budget reconciliation cannot silently drop usage.
ROUTE_NATIVE_METERS: dict[str, tuple[str, frozenset[str]]] = {
    "local_http": ("local", frozenset()),
    "owned_proxy_http": ("local", frozenset()),
    "local_browser": ("local", frozenset()),
    "brightdata_unlocker": ("brightdata", frozenset({"requests"})),
    "firecrawl_scrape": ("firecrawl", frozenset({"credits"})),
    "firecrawl_interact": ("firecrawl", frozenset({"credits"})),
    "browserbase_session": (
        "browserbase", frozenset({"browserMinutes", "proxyBytes"}),
    ),
}


class ProviderProtocolError(RuntimeError):
    """A provider reported an impossible native-meter reconciliation."""

    code = "provider_protocol_error"


@dataclass(frozen=True)
class NativeCost:
    values: Mapping[str, int | float]
    estimated: bool = False


@dataclass(frozen=True)
class ProviderRequest:
    url: str
    route: str
    timeout_seconds: int
    only_main_content: bool
    session_profile: str | None = None


@dataclass(frozen=True)
class ProviderResult:
    raw_html: str
    final_url: str
    status_code: int | None
    native_cost: NativeCost
    remote_session_id: str | None = None


@dataclass(frozen=True)
class ProviderFailure(Exception):
    code: str
    retryable: bool
    native_cost: NativeCost
    status_code: int | None = None


class ProviderAdapter(Protocol):
    name: str
    routes: frozenset[str]

    def available(self) -> bool: ...

    def reserve_cost(self, request: ProviderRequest) -> NativeCost: ...

    async def acquire(self, request: ProviderRequest) -> ProviderResult: ...

    async def cancel(self, remote_id: str) -> None: ...
