"""Direct-egress Browserbase acquisition and human-session support."""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urljoin, urlsplit

import httpx

from app.acquisition.providers import (
    NativeCost,
    ProviderFailure,
    ProviderProtocolError,
    ProviderRequest,
    ProviderResult,
    parse_retry_after,
)
from app.acquisition.sessions import SessionHandle, SessionSnapshot
from app.scraper import MAX_DOM_BYTES, _guard_browser_websocket
from app.url_safety import UnsafeUrlError, ensure_public_url


class BrowserbaseAPI:
    """Small HTTP boundary; live and connection URLs never leave memory."""

    def __init__(self, api_key: str, *, base_url: str = "https://api.browserbase.com",
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Browserbase API URL must be HTTPS without credentials")
        self._key = api_key
        self._base_url = base_url.rstrip("/") + "/"
        self._transport = transport

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        async with httpx.AsyncClient(
            transport=self._transport, timeout=30, trust_env=False,
            follow_redirects=False,
        ) as client:
            response = await client.request(
                method, urljoin(self._base_url, path.lstrip("/")),
                headers={"x-bb-api-key": self._key}, **kwargs,
            )
        if response.status_code in {401, 403}:
            raise ProviderFailure("provider_auth", False, NativeCost({}), response.status_code)
        if response.status_code == 402:
            raise ProviderFailure("provider_budget", False, NativeCost({}), 402)
        if response.status_code == 429 or response.status_code >= 500:
            raise ProviderFailure(
                "provider_unavailable", True, NativeCost({}), response.status_code,
                parse_retry_after(response.headers.get("retry-after"))
                if response.status_code == 429 else None,
            )
        if response.status_code >= 400:
            raise ProviderFailure("provider_request", False, NativeCost({}), response.status_code)
        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderProtocolError("Browserbase returned invalid JSON") from exc

    async def create(self, project_id: str, timeout_seconds: int) -> Mapping[str, Any]:
        return await self._request("POST", "/v1/sessions", json={
            "projectId": project_id,
            "timeout": timeout_seconds,
            "keepAlive": False,
            "proxies": False,
        })

    async def inspect(self, session_id: str) -> Mapping[str, Any]:
        return await self._request("GET", f"/v1/sessions/{session_id}")

    async def live_access(self, session_id: str) -> Mapping[str, Any]:
        return await self._request("GET", f"/v1/sessions/{session_id}/debug")

    async def delete(self, session_id: str) -> None:
        await self._request("DELETE", f"/v1/sessions/{session_id}")


class PlaywrightCapture:
    """Connect to one ephemeral CDP URL and capture a bounded public page."""

    async def capture(self, connect_url: str, target_url: str,
                      timeout_seconds: int) -> tuple[str, str, int | None]:
        from playwright.async_api import async_playwright

        manager = await async_playwright().start()
        browser = None
        try:
            browser = await manager.chromium.connect_over_cdp(connect_url)
            context = browser.contexts[0] if browser.contexts else await browser.new_context(
                service_workers="block", ignore_https_errors=False,
            )

            async def guard(route: Any) -> None:
                request_url = route.request.url
                try:
                    if not request_url.startswith(("http://", "https://")):
                        raise UnsafeUrlError("unsupported browser request scheme")
                    await ensure_public_url(request_url)
                except (UnsafeUrlError, ValueError):
                    await route.abort("blockedbyclient")
                    return
                await route.continue_()

            await context.route("**/*", guard)
            await context.route_web_socket("**/*", _guard_browser_websocket)
            page = await context.new_page()
            response = await page.goto(
                target_url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000,
            )
            html = await page.content()
            if len(html.encode("utf-8")) > MAX_DOM_BYTES:
                raise ValueError("Rendered DOM exceeds byte limit")
            return html, page.url, response.status if response else None
        finally:
            if browser is not None:
                await browser.close()
            await manager.stop()


class BrowserbaseAdapter:
    name = "browserbase"
    routes = frozenset({"browserbase_session"})

    def __init__(self, api_key: str, project_id: str, *, api: Any | None = None,
                 playwright: Any | None = None) -> None:
        self._api_key = api_key
        self._project_id = project_id
        self._api = api or BrowserbaseAPI(api_key)
        self._playwright = playwright or PlaywrightCapture()

    def available(self) -> bool:
        return bool(self._api_key and self._project_id)

    @staticmethod
    def validate_budget(values: Mapping[str, int | float]) -> None:
        if set(values) != {"browserMinutes", "proxyBytes"}:
            raise ValueError("Browserbase budget requires browserMinutes and proxyBytes")
        if values["proxyBytes"] != 0:
            raise ValueError("Browserbase managed proxies are disabled")
        minutes = values["browserMinutes"]
        if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or minutes <= 0:
            raise ValueError("Browserbase browserMinutes must be positive")

    def reserve_cost(self, request: ProviderRequest) -> NativeCost:
        self._validate_request(request)
        return NativeCost({
            "browserMinutes": math.ceil(request.timeout_seconds / 60),
            "proxyBytes": 0,
        })

    @staticmethod
    def _validate_request(request: ProviderRequest) -> None:
        if request.route != "browserbase_session":
            raise ValueError("Browserbase supports only browserbase_session")
        if not 60 <= request.timeout_seconds <= 21_600:
            raise ValueError("Browserbase timeout must be between 60 and 21600 seconds")

    async def acquire(self, request: ProviderRequest) -> ProviderResult:
        self._validate_request(request)
        await ensure_public_url(request.url)
        reserved = self.reserve_cost(request)
        session_id = None
        try:
            created = await self._api.create(self._project_id, request.timeout_seconds)
            session_id = created.get("id") if isinstance(created, Mapping) else None
            connect_url = created.get("connectUrl") if isinstance(created, Mapping) else None
            if not isinstance(session_id, str) or not isinstance(connect_url, str):
                raise ProviderProtocolError("Browserbase omitted session connection data")
            html, final_url, status_code = await asyncio.wait_for(
                self._playwright.capture(connect_url, request.url, request.timeout_seconds),
                timeout=request.timeout_seconds,
            )
            await ensure_public_url(final_url)
            usage = created.get("browserMinutes", reserved.values["browserMinutes"])
            if isinstance(usage, bool) or not isinstance(usage, (int, float)):
                raise ProviderProtocolError("Browserbase returned invalid browser usage")
            if usage < 0 or usage > reserved.values["browserMinutes"]:
                raise ProviderProtocolError("Browserbase usage exceeded its reservation")
            return ProviderResult(
                raw_html=html, final_url=final_url, status_code=status_code,
                native_cost=NativeCost({"browserMinutes": usage, "proxyBytes": 0}),
                remote_session_id=None,
            )
        except ProviderFailure as exc:
            if exc.native_cost.values:
                raise
            raise ProviderFailure(
                exc.code, exc.retryable,
                NativeCost(reserved.values, estimated=True), exc.status_code,
            ) from exc
        except ProviderProtocolError:
            raise
        except UnsafeUrlError:
            raise
        except Exception as exc:
            raise ProviderFailure(
                "provider_browser_failure", True, reserved,
            ) from exc
        finally:
            if session_id is not None:
                try:
                    await self._api.delete(session_id)
                except Exception:
                    pass

    async def cancel(self, remote_id: str) -> None:
        await self._api.delete(remote_id)


class BrowserbaseSessionBackend:
    """Provider-neutral live-session facade without durable upstream URLs."""

    def __init__(self, adapter: BrowserbaseAdapter) -> None:
        self._adapter = adapter
        self._active: dict[Any, _BrowserbaseLive] = {}

    async def create(self, handle: SessionHandle, target_url: str,
                     profile_state: bytes | None) -> SessionSnapshot:
        del profile_state
        await ensure_public_url(target_url)
        timeout = max(60, min(21_600, math.ceil(
            (handle.expires_at - datetime.now(timezone.utc)).total_seconds()
        )))
        created = await self._adapter._api.create(self._adapter._project_id, timeout)
        remote_id = created.get("id") if isinstance(created, Mapping) else None
        if not isinstance(remote_id, str):
            raise ProviderProtocolError("Browserbase omitted session ID")
        connect_url = created.get("connectUrl") if isinstance(created, Mapping) else None
        if not isinstance(connect_url, str) or not connect_url:
            await self._adapter._api.delete(remote_id)
            raise ProviderProtocolError("Browserbase omitted session connection URL")
        self._active[handle.id] = _BrowserbaseLive(remote_id, connect_url, target_url)
        return SessionSnapshot("waiting", handle.expires_at, {"browserMinutes": 0})

    async def inspect(self, handle: SessionHandle) -> SessionSnapshot:
        active = self._active.get(handle.id)
        if active is None:
            raise KeyError("Browserbase session is not active")
        result = await self._adapter._api.inspect(active.remote_id)
        status = result.get("status", "waiting") if isinstance(result, Mapping) else "waiting"
        minutes = result.get("browserMinutes", 0) if isinstance(result, Mapping) else 0
        return SessionSnapshot(str(status), handle.expires_at, {"browserMinutes": minutes})

    async def token(self, handle: SessionHandle) -> str:
        active = self._active.get(handle.id)
        if active is None:
            raise KeyError("Browserbase session is not active")
        result = await self._adapter._api.live_access(active.remote_id)
        token = result.get("token") if isinstance(result, Mapping) else None
        if not isinstance(token, str) or not token:
            raise ProviderProtocolError("Browserbase omitted live access token")
        return token

    async def send(self, handle: SessionHandle, action: Mapping[str, object]) -> object:
        del handle, action
        raise NotImplementedError("Browserbase live actions use the scoped live-view token")

    async def resume(self, handle: SessionHandle) -> ProviderResult:
        active = self._active.get(handle.id)
        if active is None:
            raise KeyError("Browserbase session is not active")
        timeout = max(1, math.ceil(
            (handle.expires_at - datetime.now(timezone.utc)).total_seconds()
        ))
        try:
            html, final_url, status_code = await asyncio.wait_for(
                self._adapter._playwright.capture(
                    active.connect_url, active.target_url, timeout,
                ),
                timeout=timeout,
            )
            await ensure_public_url(final_url)
            usage = await self._adapter._api.inspect(active.remote_id)
            minutes = usage.get("browserMinutes", math.ceil(timeout / 60))
            if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or minutes < 0:
                raise ProviderProtocolError("Browserbase returned invalid browser usage")
            return ProviderResult(
                html, final_url, status_code,
                NativeCost({"browserMinutes": minutes, "proxyBytes": 0}),
            )
        finally:
            await self.close(handle)

    async def close(self, handle: SessionHandle) -> None:
        active = self._active.pop(handle.id, None)
        if active is not None:
            await self._adapter._api.delete(active.remote_id)


@dataclass(frozen=True)
class _BrowserbaseLive:
    remote_id: str
    connect_url: str
    target_url: str
