"""Deterministic durable-acquisition route selection and attempt accounting."""
from __future__ import annotations

from typing import Any, Mapping

from app.acquisition.local import LocalAdapter
from app.acquisition.providers import (
    NativeCost,
    ProviderFailure,
    ProviderProtocolError,
    ProviderRequest,
    ProviderResult,
)
from app.acquisition.registry import ProviderRegistry, ProviderUnavailable
from app.crawl.types import ClaimedTask, TaskResult
from app.url_safety import UnsafeUrlError, ensure_public_url


_ROUTES = {
    "ordinary": ("local_http",),
    "static_block": ("owned_proxy_http", "brightdata_unlocker"),
    "rendering": ("local_browser", "firecrawl_scrape"),
    "interactive": ("browserbase_session", "firecrawl_interact"),
}


def routes_for(capability: str) -> list[str]:
    try:
        return list(_ROUTES[capability])
    except KeyError as exc:
        raise ValueError("unknown acquisition capability") from exc


class AcquisitionRouter:
    """Run only classified fallback routes while one task owns its origin permit."""

    def __init__(self, registry: ProviderRegistry, repository: Any, scraper: Any) -> None:
        self._registry = registry
        self._repository = repository
        self._scraper = scraper

    async def acquire(self, task: ClaimedTask, *, capability: str | None = None) -> TaskResult:
        acquisition = task.config.get("acquisition")
        acquisition = acquisition if isinstance(acquisition, Mapping) else {}
        provider = acquisition.get("provider", "auto")
        provider = provider if isinstance(provider, str) else "auto"
        capability = capability or self._capability(task.config, acquisition)
        try:
            await ensure_public_url(task.url)
        except UnsafeUrlError as exc:
            raise ProviderFailure("unsafe_request_url", False, NativeCost({})) from exc
        routes = self._routes(provider, capability)
        if not routes:
            raise ProviderUnavailable("provider_unavailable")
        request = ProviderRequest(
            url=task.url,
            route="", timeout_seconds=int(task.config.get("timeoutSeconds", 60)),
            only_main_content=bool(task.config.get("onlyMainContent", True)),
            session_profile=acquisition.get("sessionProfile")
            if isinstance(acquisition.get("sessionProfile"), str) else None,
        )
        last_failure: ProviderFailure | None = None
        for route in routes:
            if not self._registry.route_available(route):
                continue
            adapter = self._registry.adapter_for_route(route)
            route_request = ProviderRequest(
                request.url, route, request.timeout_seconds, request.only_main_content,
                request.session_profile,
            )
            try:
                reserved = adapter.reserve_cost(route_request)
            except ValueError as exc:
                raise ProviderFailure("provider_request", False, NativeCost({})) from exc
            attempt = await self._repository.reserve_acquisition_attempt(
                task.id, task.lease_token, route, reserved.values,
            )
            if attempt is None:
                raise ProviderUnavailable("provider_budget_exhausted")
            result: ProviderResult | None = None
            failure: ProviderFailure | None = None
            try:
                if route == "owned_proxy_http" and isinstance(adapter, LocalAdapter):
                    result = await adapter.acquire_owned_proxy(
                        route_request, origin_key=task.origin_key, task_id=task.id,
                        lease_token=task.lease_token,
                    )
                else:
                    result = await adapter.acquire(route_request)
                await ensure_public_url(result.final_url)
                return self._normalize(result, route_request.only_main_content, adapter.name)
            except ProviderFailure as exc:
                failure = exc
                last_failure = exc
                if exc.code == "provider_protocol_error":
                    self._registry.unhealthy(adapter.name)
                if not exc.retryable:
                    raise
            except UnsafeUrlError as exc:
                failure = ProviderFailure("unsafe_final_url", False, reserved)
                last_failure = failure
                raise failure from exc
            except ProviderProtocolError as exc:
                self._registry.unhealthy(adapter.name)
                failure = ProviderFailure("provider_protocol_error", False, reserved)
                last_failure = failure
                raise failure from exc
            except Exception as exc:
                self._registry.unhealthy(adapter.name)
                failure = ProviderFailure("provider_protocol_error", False, reserved)
                last_failure = failure
                raise failure from exc
            finally:
                actual = result.native_cost if result is not None else (
                    failure.native_cost if failure is not None else reserved
                )
                outcome = "succeeded" if failure is None and result is not None else (
                    "retryable_failure" if failure is not None and failure.retryable else "failed"
                )
                try:
                    await self._repository.finish_acquisition_attempt(
                        attempt.id, task.lease_token, outcome, actual.values,
                        cost_estimated=actual.estimated,
                    )
                except ProviderProtocolError as exc:
                    self._registry.unhealthy(adapter.name)
                    raise ProviderFailure(
                        "provider_protocol_error", False, reserved,
                    ) from exc
                finally:
                    if result is not None and result.remote_session_id:
                        try:
                            await adapter.cancel(result.remote_session_id)
                        except Exception:
                            pass
            if failure is None or not failure.retryable:
                break
        if last_failure is not None:
            raise last_failure
        raise ProviderUnavailable("provider_unavailable")

    def _routes(self, provider: str, capability: str) -> list[str]:
        if provider == "auto":
            return [route for route in routes_for(capability) if self._registry.route_available(route)]
        self._registry.require_available(provider)
        if provider == "local":
            return ["local_browser" if capability == "rendering" else "local_http"]
        if provider == "brightdata":
            return ["brightdata_unlocker"]
        if provider == "browserbase":
            return ["browserbase_session"]
        if provider == "firecrawl":
            return ["firecrawl_interact" if capability == "interactive" else "firecrawl_scrape"]
        raise ProviderUnavailable("provider_unavailable")

    @staticmethod
    def _capability(config: Mapping[str, Any], acquisition: Mapping[str, Any]) -> str:
        if acquisition.get("allowHumanIntervention") is True:
            return "interactive"
        return "rendering" if config.get("engine") == "browser" else "ordinary"

    def _normalize(self, result: ProviderResult, only_main_content: bool, engine: str) -> TaskResult:
        built = self._scraper._build_result(
            result.raw_html, result.final_url, only_main_content,
            engine_used=engine, status_code=result.status_code,
        )
        metadata = built.get("metadata")
        return TaskResult(
            final_url=result.final_url, status_code=result.status_code,
            title=str(built.get("title", "")), markdown=str(built.get("markdown", "")),
            metadata=metadata if isinstance(metadata, Mapping) else {},
        )
