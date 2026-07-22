"""Fixed-credit Firecrawl scrape and code-only interactive acquisition."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import quote, urljoin, urlsplit
from uuid import UUID

import httpx

from app.acquisition.providers import NativeCost, ProviderFailure, ProviderRequest, ProviderResult
from app.acquisition.sessions import SessionHandle, SessionSnapshot, SessionStateError
from app.crawl.types import TaskResult
from app.scraper import MAX_DOM_BYTES, WebScraper
from app.url_safety import ensure_public_url


MAX_INTERACT_CODE_CHARS = 16 * 1024


def _cost(credits: int | float, *, estimated: bool = False) -> NativeCost:
    return NativeCost({"credits": credits}, estimated=estimated)


def _failure(code: str, retryable: bool, *, status_code: int | None = None,
             credits: int | float = 1) -> ProviderFailure:
    return ProviderFailure(code, retryable, _cost(credits), status_code)


def _interact_credits(timeout_seconds: int) -> int:
    return 2 * max(1, math.ceil(timeout_seconds / 60))


def _data(body: bytes, status_code: int, credits: int | float, *,
          allow_empty: bool = False) -> Mapping[str, Any]:
    if allow_empty and not body:
        return {}
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, ValueError):
        raise _failure("provider_protocol_error", False, status_code=status_code,
                       credits=credits) from None
    if not isinstance(payload, Mapping) or payload.get("success") is not True:
        raise _failure("provider_protocol_error", False, status_code=status_code,
                       credits=credits)
    data = payload.get("data")
    if allow_empty and data is None:
        return {}
    if not isinstance(data, Mapping):
        raise _failure("provider_protocol_error", False, status_code=status_code,
                       credits=credits)
    return data


def _reported_result(data: Mapping[str, Any], fallback_url: str, credits: int | float) -> tuple[str, str, int | None]:
    html = data.get("rawHtml")
    if not isinstance(html, str):
        raise _failure("provider_protocol_error", False, credits=credits)
    metadata = data.get("metadata")
    reported = metadata.get("sourceURL") if isinstance(metadata, Mapping) else None
    if not isinstance(reported, str) or not reported:
        reported = data.get("url")
    final_url = reported if isinstance(reported, str) and reported else fallback_url
    status = data.get("statusCode")
    if status is not None and (isinstance(status, bool) or not isinstance(status, int)):
        raise _failure("provider_protocol_error", False, credits=credits)
    if len(html.encode("utf-8")) > MAX_DOM_BYTES:
        raise _failure("response_too_large", False, credits=credits)
    return html, final_url, status


@dataclass
class _InteractiveSession:
    remote_id: str
    target_url: str
    reserved_credits: int
    cost: NativeCost
    closed: bool = False


class FirecrawlAdapter:
    """Firecrawl's cache-disabled raw-HTML scrape endpoint."""

    name = "firecrawl"
    routes = frozenset({"firecrawl_scrape", "firecrawl_interact"})

    def __init__(self, api_key: str | None, *, base_url: str = "https://api.firecrawl.dev",
                 transport: httpx.AsyncBaseTransport | None = None):
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Firecrawl API URL must be HTTPS without credentials")
        self._api_key = (api_key or "").strip()
        self._base_url = base_url.rstrip("/") + "/"
        self._client = httpx.AsyncClient(
            transport=transport, trust_env=False, follow_redirects=False,
        )

    def available(self) -> bool:
        return bool(self._api_key)

    def reserve_cost(self, request: ProviderRequest) -> NativeCost:
        if request.route == "firecrawl_scrape":
            return _cost(1)
        if request.route == "firecrawl_interact" and request.timeout_seconds > 0:
            return _cost(_interact_credits(request.timeout_seconds))
        raise ValueError("Firecrawl route or timeout is invalid")

    async def aclose(self) -> None:
        await self._client.aclose()

    async def acquire(self, request: ProviderRequest) -> ProviderResult:
        if not self.available():
            raise _failure("provider_unavailable", False)
        if request.route != "firecrawl_scrape" or request.timeout_seconds <= 0:
            raise _failure("provider_request", False)
        await ensure_public_url(request.url)
        data = await self._request_data(
            "POST", "v2/scrape", credits=1, timeout_seconds=request.timeout_seconds,
            json={
                "url": request.url,
                "formats": ["rawHtml"],
                "maxAge": 0,
                "storeInCache": False,
                "skipTlsVerification": False,
            },
        )
        html, final_url, status = _reported_result(data, request.url, 1)
        await ensure_public_url(final_url)
        return ProviderResult(html, final_url, status, _cost(1))

    async def cancel(self, remote_id: str) -> None:
        if not remote_id:
            return
        try:
            await self._request_data("DELETE", self._session_path(remote_id), credits=1,
                                     timeout_seconds=15, allow_empty=True)
        except ProviderFailure:
            # Cancellation is best effort; callers must still release their local session.
            return

    async def _request_data(self, method: str, path: str, *, credits: int | float,
                            timeout_seconds: int, json: Mapping[str, object] | None = None,
                            allow_empty: bool = False) -> Mapping[str, Any]:
        failure: ProviderFailure | None = None
        body = bytearray()
        status_code: int | None = None
        try:
            async with self._client.stream(
                method, urljoin(self._base_url, path.lstrip("/")),
                headers={"Authorization": f"Bearer {self._api_key}"}, json=json,
                timeout=httpx.Timeout(timeout_seconds),
            ) as response:
                status_code = response.status_code
                if not 200 <= response.status_code < 300:
                    failure = self._classify_response(response.status_code, credits)
                else:
                    content_length = response.headers.get("content-length")
                    if content_length and content_length.isdigit() and int(content_length) > MAX_DOM_BYTES:
                        failure = _failure("response_too_large", False, status_code=response.status_code,
                                           credits=credits)
                    else:
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > MAX_DOM_BYTES:
                                failure = _failure("response_too_large", False,
                                                   status_code=response.status_code, credits=credits)
                                break
        except httpx.HTTPError:
            raise _failure("provider_transport", True, credits=credits) from None
        if failure is not None:
            raise failure
        assert status_code is not None
        return _data(bytes(body), status_code, credits, allow_empty=allow_empty)

    @staticmethod
    def _classify_response(status_code: int, credits: int | float) -> ProviderFailure:
        if status_code in (401, 403):
            return _failure("provider_auth", False, status_code=status_code, credits=credits)
        if status_code == 402:
            return _failure("provider_billing", False, status_code=status_code, credits=credits)
        if status_code == 429:
            return _failure("provider_rate_limited", True, status_code=status_code, credits=credits)
        if status_code >= 500:
            return _failure("provider_failure", True, status_code=status_code, credits=credits)
        return _failure("provider_request", False, status_code=status_code, credits=credits)

    @staticmethod
    def _session_path(remote_id: str) -> str:
        return "v2/scrape/" + quote(remote_id, safe="")


class FirecrawlSessionBackend:
    """Local handle map for a Firecrawl Interact session; live URLs never enter it."""

    def __init__(self, adapter: FirecrawlAdapter, *, timeout_seconds: int,
                 scraper: Any | None = None):
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._adapter = adapter
        self._timeout_seconds = timeout_seconds
        self._scraper = scraper or WebScraper()
        self._sessions: dict[UUID, _InteractiveSession] = {}

    def native_cost(self, handle: SessionHandle) -> NativeCost:
        state = self._state(handle)
        return state.cost

    async def create(self, handle: SessionHandle, target_url: str,
                     profile_state: bytes | None) -> SessionSnapshot:
        del profile_state  # Firecrawl Interact does not accept reusable cookie state.
        if handle.id in self._sessions:
            raise SessionStateError("session already exists")
        await ensure_public_url(target_url)
        reserved = _interact_credits(self._timeout_seconds)
        data = await self._adapter._request_data(
            "POST", "v2/scrape", credits=reserved, timeout_seconds=self._timeout_seconds,
            json={
                "url": target_url,
                "formats": ["rawHtml"],
                "maxAge": 0,
                "storeInCache": False,
                "skipTlsVerification": False,
            },
        )
        remote_id = data.get("id")
        if not isinstance(remote_id, str) or not remote_id:
            raise _failure("provider_protocol_error", False, credits=reserved)
        self._sessions[handle.id] = _InteractiveSession(
            remote_id=remote_id, target_url=target_url, reserved_credits=reserved,
            cost=_cost(reserved, estimated=True),
        )
        return SessionSnapshot(status="waiting", expires_at=handle.expires_at,
                               usage={"credits": reserved})

    async def inspect(self, handle: SessionHandle) -> SessionSnapshot:
        state = self._state(handle)
        return SessionSnapshot(
            status="closed" if state.closed else "waiting", expires_at=handle.expires_at,
            usage=state.cost.values,
        )

    async def send(self, handle: SessionHandle, action: Mapping[str, object]) -> object:
        state = self._state(handle)
        try:
            code = self._code(action)
            await self._adapter._request_data(
                "POST", self._adapter._session_path(state.remote_id) + "/interact",
                credits=state.reserved_credits, timeout_seconds=self._timeout_seconds,
                json={"code": code},
            )
        except BaseException:
            await self.close(handle)
            raise
        return None

    async def resume(self, handle: SessionHandle) -> TaskResult:
        state = self._state(handle)
        try:
            data = await self._adapter._request_data(
                "GET", self._adapter._session_path(state.remote_id),
                credits=state.reserved_credits, timeout_seconds=self._timeout_seconds,
            )
            html, final_url, status = _reported_result(data, state.target_url, state.reserved_credits)
            await ensure_public_url(final_url)
            built = self._build_result(html, final_url, status)
            return TaskResult(
                final_url=final_url, status_code=status, title=str(built.get("title", "")),
                markdown=str(built.get("markdown", "")),
                metadata=built.get("metadata") if isinstance(built.get("metadata"), Mapping) else {},
            )
        finally:
            await self.close(handle)

    async def close(self, handle: SessionHandle) -> None:
        state = self._state(handle)
        if state.closed:
            return
        state.closed = True
        try:
            data = await self._adapter._request_data(
                "DELETE", self._adapter._session_path(state.remote_id),
                credits=state.reserved_credits, timeout_seconds=15, allow_empty=True,
            )
        except ProviderFailure:
            return
        billed = data.get("creditsBilled")
        if billed is None:
            return
        if isinstance(billed, bool) or not isinstance(billed, (int, float)) or billed < 0:
            raise _failure("provider_protocol_error", False, credits=state.reserved_credits)
        if billed > state.reserved_credits:
            raise _failure("provider_protocol_error", False, credits=state.reserved_credits)
        state.cost = _cost(billed)

    def _build_result(self, html: str, final_url: str, status: int | None) -> Mapping[str, Any]:
        return self._scraper._build_result(
            html, final_url, True, engine_used="firecrawl", status_code=status,
        )

    def _state(self, handle: SessionHandle) -> _InteractiveSession:
        state = self._sessions.get(handle.id)
        if state is None:
            raise SessionStateError("unknown Firecrawl session")
        return state

    @staticmethod
    def _code(action: Mapping[str, object]) -> str:
        if not isinstance(action, Mapping) or set(action) != {"code"}:
            raise _failure("provider_request", False)
        code = action["code"]
        if not isinstance(code, str) or not code.strip() or len(code) > MAX_INTERACT_CODE_CHARS:
            raise _failure("provider_request", False)
        return code
