"""Tests for X-API-Key auth, CORS, and the bind-policy check.

Hermetic: the API is driven over ASGI; the bind-policy guard is a pure function.
"""
import importlib

import httpx
import pytest


def _fresh_app(monkeypatch, **env):
    """Reload app.main with a clean auth environment so module-level
    APP_PASSWORD/API_KEYS pick up the test values."""
    for k in ("APP_PASSWORD", "API_KEYS", "APP_USERNAME", "ALLOW_UNAUTHENTICATED",
              "CORS_ORIGINS", "PUBLISHED_BIND_ADDRESS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import app.main as main
    return importlib.reload(main)


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")


# --- bind policy (pure) ----------------------------------------------------
def test_bind_policy_loopback_ok(monkeypatch):
    main = _fresh_app(monkeypatch)
    assert main.check_bind_policy(host="127.0.0.1", has_auth=False, allow_unauth=False) == "ok"
    assert main.check_bind_policy(host="localhost", has_auth=False, allow_unauth=False) == "ok"


def test_bind_policy_non_loopback_without_auth_refuses(monkeypatch):
    main = _fresh_app(monkeypatch)
    with pytest.raises(RuntimeError):
        main.check_bind_policy(host="0.0.0.0", has_auth=False, allow_unauth=False)


def test_bind_policy_non_loopback_with_auth_ok(monkeypatch):
    main = _fresh_app(monkeypatch)
    assert main.check_bind_policy(host="0.0.0.0", has_auth=True, allow_unauth=False) == "ok"


def test_bind_policy_allow_unauth_downgrades_to_warning(monkeypatch):
    main = _fresh_app(monkeypatch)
    assert main.check_bind_policy(host="0.0.0.0", has_auth=False, allow_unauth=True) == "warned"


def test_is_loopback_host(monkeypatch):
    main = _fresh_app(monkeypatch)
    assert main._is_loopback_host("127.0.0.1")
    assert main._is_loopback_host("::1")
    assert not main._is_loopback_host("0.0.0.0")
    assert not main._is_loopback_host("192.168.1.5")


def test_published_bind_address_overrides_container_listener(monkeypatch):
    main = _fresh_app(monkeypatch, PUBLISHED_BIND_ADDRESS="127.0.0.1")
    monkeypatch.setattr(main.sys, "argv", ["uvicorn", "--host", "0.0.0.0"])
    assert main._bind_host() == "127.0.0.1"


# --- auth middleware -------------------------------------------------------
async def test_open_when_no_auth_configured(monkeypatch):
    main = _fresh_app(monkeypatch)
    async with _client(main.app) as c:
        assert (await c.get("/api/artifacts")).status_code == 200


async def test_health_always_open(monkeypatch):
    main = _fresh_app(monkeypatch, API_KEYS="secret123")
    async with _client(main.app) as c:
        assert (await c.get("/api/health")).status_code == 200


async def test_api_key_required_and_accepted(monkeypatch):
    main = _fresh_app(monkeypatch, API_KEYS="secret123,other")
    async with _client(main.app) as c:
        assert (await c.get("/api/artifacts")).status_code == 401
        ok = await c.get("/api/artifacts", headers={"X-API-Key": "secret123"})
        assert ok.status_code == 200
        ok2 = await c.get("/api/artifacts", headers={"X-API-Key": "other"})
        assert ok2.status_code == 200
        bad = await c.get("/api/artifacts", headers={"X-API-Key": "nope"})
        assert bad.status_code == 401


async def test_basic_auth_still_works(monkeypatch):
    main = _fresh_app(monkeypatch, APP_PASSWORD="pw")
    import base64
    tok = base64.b64encode(b"admin:pw").decode()
    async with _client(main.app) as c:
        assert (await c.get("/api/artifacts")).status_code == 401
        ok = await c.get("/api/artifacts", headers={"Authorization": f"Basic {tok}"})
        assert ok.status_code == 200


async def test_either_credential_satisfies(monkeypatch):
    main = _fresh_app(monkeypatch, APP_PASSWORD="pw", API_KEYS="k1")
    async with _client(main.app) as c:
        assert (await c.get("/api/artifacts", headers={"X-API-Key": "k1"})).status_code == 200
    # restore default app state for other tests
    _fresh_app(monkeypatch)


async def test_cors_is_opt_in(monkeypatch):
    main = _fresh_app(monkeypatch)
    async with _client(main.app) as c:
        response = await c.get("/api/health", headers={"Origin": "https://client.example"})
        assert "access-control-allow-origin" not in response.headers

    main = _fresh_app(monkeypatch, CORS_ORIGINS="https://client.example")
    async with _client(main.app) as c:
        response = await c.get("/api/health", headers={"Origin": "https://client.example"})
        assert response.headers["access-control-allow-origin"] == "https://client.example"


async def test_unsafe_target_is_a_bad_request(monkeypatch):
    from app.url_safety import UnsafeUrlError

    main = _fresh_app(monkeypatch)

    async def reject(**kwargs):
        raise UnsafeUrlError("Refusing to access a non-public network address")

    monkeypatch.setattr(main.scraper, "scrape", reject)
    async with _client(main.app) as c:
        response = await c.post("/api/scrape", json={"url": "http://127.0.0.1"})
    assert response.status_code == 400
    assert "non-public" in response.json()["detail"]


async def test_artifacts_page_escapes_scraped_fields(monkeypatch):
    main = _fresh_app(monkeypatch)
    payload = "<script>alert(1)</script>"
    monkeypatch.setattr(main.storage, "list_artifacts", lambda: [{
        "kind": "scrape", "title": payload, "url": payload, "pages": 1,
        "bytes": 12, "mtime": 0, "md": "/data/x.md", "json": "/data/x.json",
    }])
    html = await main.artifacts_page()
    assert payload not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
