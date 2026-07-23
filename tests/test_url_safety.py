import pytest
from curl_cffi import CurlOpt

from app import fetch, scraper, url_safety


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1",
        "http://10.0.0.1",
        "http://169.254.169.254/latest/meta-data",
        "http://100.64.0.1",
        "http://0.0.0.0",
        "http://[::1]",
        "http://[fc00::1]",
        "http://[fe80::1]",
        "http://[::ffff:127.0.0.1]",
        "http://[::ffff:93.184.216.34]",
        "http://[ff02::1]",
        "http://[fec0::1]",
        "http://[::7f00:1]",
        "http://[64:ff9b::7f00:1]",
        "http://[2002:7f00:1::]",
        "http://localhost",
    ],
)
async def test_private_targets_are_blocked(monkeypatch, url):
    monkeypatch.delenv("ALLOW_PRIVATE_NETWORKS", raising=False)
    with pytest.raises(url_safety.UnsafeUrlError):
        await url_safety.ensure_public_url(url)


async def test_dns_answer_must_be_public(monkeypatch):
    async def private_answer(host, port):
        return [(None, None, None, None, ("192.168.1.20", port))]

    monkeypatch.setattr(url_safety, "_resolve", private_answer)
    with pytest.raises(url_safety.UnsafeUrlError):
        await url_safety.ensure_public_url("https://internal.example")


async def test_mixed_dns_answer_is_blocked(monkeypatch):
    async def mixed_answer(host, port):
        return [
            (None, None, None, None, ("93.184.216.34", port)),
            (None, None, None, None, ("10.0.0.5", port)),
        ]

    monkeypatch.setattr(url_safety, "_resolve", mixed_answer)
    with pytest.raises(url_safety.UnsafeUrlError):
        await url_safety.ensure_public_url("https://mixed.example")


@pytest.mark.parametrize("records", [[], OSError("dns failed")])
async def test_dns_failure_is_closed(monkeypatch, records):
    async def resolve(host, port):
        if isinstance(records, Exception):
            raise records
        return records

    monkeypatch.setattr(url_safety, "_resolve", resolve)
    with pytest.raises(url_safety.UnsafeUrlError):
        await url_safety.ensure_public_url("https://missing.example")


@pytest.mark.parametrize(
    "url", ["https://93.184.216.34", "https://[2001:4860:4860::8888]"]
)
async def test_public_literal_is_accepted(monkeypatch, url):
    monkeypatch.delenv("ALLOW_PRIVATE_NETWORKS", raising=False)
    assert await url_safety.ensure_public_url(url)


async def test_private_network_opt_in_is_explicit(monkeypatch):
    monkeypatch.setenv("ALLOW_PRIVATE_NETWORKS", "true")
    await url_safety.ensure_public_url("http://127.0.0.1")


async def test_http_redirect_is_revalidated_and_addresses_share_resolve_entry(monkeypatch):
    class Response:
        status_code = 302
        headers = {"location": "http://127.0.0.1/admin"}
        text = ""
        content = b""
        url = "https://public.example"
        primary_ip = "93.184.216.34"

    class Session:
        last = None

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.curl_options = {}
            self.get_kwargs = []
            Session.last = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            self.get_kwargs.append(kwargs)
            return Response()

    checked = []

    async def validate(url):
        checked.append(url)
        if "127.0.0.1" in url:
            raise url_safety.UnsafeUrlError("private")
        return ("93.184.216.34", "2001:4860:4860::8888")

    monkeypatch.setattr(fetch, "AsyncSession", Session)
    monkeypatch.setattr(fetch, "ensure_public_url", validate)

    with pytest.raises(url_safety.UnsafeUrlError):
        await fetch.fetch_http("https://public.example")
    assert checked == ["https://public.example", "http://127.0.0.1/admin"]
    assert Session.last.kwargs["trust_env"] is False
    assert Session.last.get_kwargs == [{"timeout": 20, "allow_redirects": False}]
    assert Session.last.curl_options[CurlOpt.RESOLVE] == [
        "public.example:443:93.184.216.34,[2001:4860:4860::8888]"
    ]


async def test_http_ip_literal_does_not_use_curl_resolve(monkeypatch):
    class Response:
        status_code = 200
        headers = {"content-type": "text/html"}
        text = "ok"
        content = b"ok"
        url = "https://[2001:4860:4860::8888]/"
        primary_ip = "2001:4860:4860::8888"

    class Session:
        last = None

        def __init__(self, **kwargs):
            self.curl_options = {CurlOpt.RESOLVE: ["stale"]}
            Session.last = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            return Response()

    monkeypatch.setattr(fetch, "AsyncSession", Session)
    result = await fetch.fetch_http("https://[2001:4860:4860::8888]/")
    assert result["status"] == 200
    assert Session.last.curl_options == {}


async def test_browser_websocket_guard_closes_private_target(monkeypatch):
    class WebSocketRoute:
        url = "ws://127.0.0.1/socket"
        closed = None
        connected = False

        async def close(self, **kwargs):
            self.closed = kwargs

        def connect_to_server(self):
            self.connected = True

    async def reject(url):
        raise url_safety.UnsafeUrlError("private")

    monkeypatch.setattr(scraper, "ensure_public_url", reject)
    route = WebSocketRoute()
    assert await scraper._guard_browser_websocket(route) is False
    assert route.closed["code"] == 1008
    assert route.connected is False
