from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from app.acquisition.browserbase import (
    BrowserbaseAPI,
    BrowserbaseAdapter,
    BrowserbaseSessionBackend,
)
from app.acquisition.providers import ProviderFailure, ProviderRequest
from app.acquisition.sessions import SessionHandle


def _request(timeout_seconds=60):
    return ProviderRequest(
        url="https://example.com", route="browserbase_session",
        timeout_seconds=timeout_seconds, only_main_content=True,
    )


class FakeAPI:
    def __init__(self):
        self.deleted_sessions = []
        self.created = []

    async def create(self, project_id, timeout_seconds):
        self.created.append((project_id, timeout_seconds))
        return {"id": "session-1", "connectUrl": "wss://connect.invalid", "browserMinutes": 1}

    async def delete(self, session_id):
        self.deleted_sessions.append(session_id)

    async def inspect(self, session_id):
        return {"id": session_id, "status": "RUNNING", "browserMinutes": 0.5}

    async def live_access(self, session_id):
        return {"id": session_id, "token": "one-upstream-token"}


class FakePlaywright:
    def __init__(self, error=None):
        self.error = error

    async def capture(self, connect_url, target_url, timeout_seconds):
        assert connect_url == "wss://connect.invalid"
        assert target_url == "https://example.com"
        assert timeout_seconds == 60
        if self.error:
            raise self.error
        return "<html>ok</html>", "https://example.com/final", 200


def test_browserbase_rejects_proxy_budget():
    with pytest.raises(ValueError, match="managed proxies are disabled"):
        BrowserbaseAdapter.validate_budget({"browserMinutes": 5, "proxyBytes": 1})


@pytest.mark.asyncio
async def test_browserbase_api_disables_proxies_and_accepts_empty_delete():
    requests = []

    async def handle(request):
        requests.append(request)
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(200, json={"id": "session-1", "connectUrl": "wss://cdp"})

    api = BrowserbaseAPI("secret", transport=httpx.MockTransport(handle))
    await api.create("project", 60)
    await api.delete("session-1")

    assert json.loads(requests[0].content) == {
        "projectId": "project", "timeout": 60, "keepAlive": False, "proxies": False,
    }
    assert requests[0].headers["x-bb-api-key"] == "secret"


@pytest.mark.asyncio
async def test_browserbase_api_normalizes_retry_after():
    api = BrowserbaseAPI(
        "secret",
        transport=httpx.MockTransport(lambda _request: httpx.Response(
            429, headers={"retry-after": "9999999999"},
        )),
    )
    with pytest.raises(ProviderFailure) as error:
        await api.create("project", 60)
    assert error.value.retry_after_seconds == 3600


@pytest.mark.asyncio
async def test_browserbase_always_terminates_session(monkeypatch):
    async def public(_url):
        return ("93.184.216.34",)

    monkeypatch.setattr("app.acquisition.browserbase.ensure_public_url", public)
    api = FakeAPI()
    adapter = BrowserbaseAdapter(
        "key", "project", api=api, playwright=FakePlaywright(RuntimeError("capture failed")),
    )

    with pytest.raises(ProviderFailure, match="provider_browser_failure"):
        await adapter.acquire(_request())
    assert api.deleted_sessions == ["session-1"]


@pytest.mark.asyncio
async def test_browserbase_uses_direct_egress_and_bounded_native_usage(monkeypatch):
    checked = []

    async def public(url):
        checked.append(url)
        return ("93.184.216.34",)

    monkeypatch.setattr("app.acquisition.browserbase.ensure_public_url", public)
    api = FakeAPI()
    adapter = BrowserbaseAdapter("key", "project", api=api, playwright=FakePlaywright())

    result = await adapter.acquire(_request())

    assert api.created == [("project", 60)]
    assert api.deleted_sessions == ["session-1"]
    assert checked == ["https://example.com", "https://example.com/final"]
    assert result.native_cost.values == {"browserMinutes": 1, "proxyBytes": 0}
    assert result.remote_session_id is None


@pytest.mark.asyncio
async def test_live_urls_are_not_exposed_by_session_snapshot(monkeypatch):
    async def public(_url):
        return ("93.184.216.34",)

    monkeypatch.setattr("app.acquisition.browserbase.ensure_public_url", public)
    api = FakeAPI()
    backend = BrowserbaseSessionBackend(
        BrowserbaseAdapter("key", "project", api=api, playwright=FakePlaywright())
    )
    handle = SessionHandle(
        id=__import__("uuid").uuid4(), backend="browserbase",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    snapshot = await backend.create(handle, "https://example.com", None)
    inspected = await backend.inspect(handle)
    token = await backend.token(handle)
    await backend.close(handle)

    assert snapshot.status == "waiting" and inspected.status == "RUNNING"
    assert token == "one-upstream-token"
    assert not {"connectUrl", "liveUrl", "cdpUrl"} & set(snapshot.__dict__)
    assert api.deleted_sessions == ["session-1"]


@pytest.mark.asyncio
async def test_live_resume_captures_and_closes(monkeypatch):
    async def public(_url):
        return ("93.184.216.34",)

    monkeypatch.setattr("app.acquisition.browserbase.ensure_public_url", public)
    api = FakeAPI()
    backend = BrowserbaseSessionBackend(
        BrowserbaseAdapter("key", "project", api=api, playwright=FakePlaywright())
    )
    handle = SessionHandle(
        id=__import__("uuid").uuid4(), backend="browserbase",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
    )

    await backend.create(handle, "https://example.com", None)
    result = await backend.resume(handle)

    assert result.raw_html == "<html>ok</html>"
    assert result.final_url == "https://example.com/final"
    assert api.deleted_sessions == ["session-1"]
