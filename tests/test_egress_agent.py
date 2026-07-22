import asyncio
import base64
import importlib.util
import json
from pathlib import Path
import ssl
from uuid import uuid4

import pytest

from app.acquisition.proxy import MtlsConnectBridge, ProxyPool, load_configured_proxies
from app.acquisition.proxy import ProxyLease
from app.crawl.types import ClaimedTask
from app.crawl.worker import CrawlWorker
from app.scraper import WebScraper
from tests.conftest import requires_db

from app.egress_agent import (
    ConnectError,
    EgressAgent,
    EgressConfig,
    parse_connect,
    validate_addresses,
    validate_connect_target,
)


def test_parse_connect_accepts_authority_only():
    assert parse_connect(
        b"CONNECT Example.com:443 HTTP/1.1\r\nHost: example.com\r\n\r\n"
    ) == ("example.com", 443)

    for request in (
        b"GET http://example.com/ HTTP/1.1\r\n\r\n",
        b"CONNECT example.com:443 HTTP/1.0\r\n\r\n",
        b"CONNECT user@example.com:443 HTTP/1.1\r\n\r\n",
        b"CONNECT example.com:8080 HTTP/1.1\r\n\r\n",
        b"CONNECT example.com:443 HTTP/1.1\r\nX-Forwarded-For: 1.2.3.4\r\n\r\n",
        b"CONNECT bad_host:443 HTTP/1.1\r\nHost: bad_host\r\n\r\n",
        b"CONNECT example.com:443 HTTP/1.1\r\n\r\nbody",
    ):
        with pytest.raises(ConnectError):
            parse_connect(request)


@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.0.0.1", "169.254.169.254", "224.0.0.1", "::1", "fe80::1",
])
def test_agent_rejects_non_public_addresses(address):
    with pytest.raises(ConnectError, match="public"):
        validate_addresses([address])


async def test_validate_target_rejects_mixed_dns_answer(monkeypatch):
    loop = asyncio.get_running_loop()

    async def getaddrinfo(*args, **kwargs):
        return [
            (0, 0, 0, "", ("93.184.216.34", 443)),
            (0, 0, 0, "", ("10.0.0.1", 443)),
        ]

    monkeypatch.setattr(loop, "getaddrinfo", getaddrinfo)
    with pytest.raises(ConnectError, match="public"):
        await validate_connect_target("example.com", 443)


async def test_agent_connects_to_validated_address_without_reresolving(monkeypatch):
    connected = {}

    async def validate(host, port):
        assert (host, port) == ("example.com", 443)
        return ("93.184.216.34",)

    class Reader:
        def __init__(self, chunks):
            self.chunks = list(chunks)

        async def readuntil(self, marker):
            assert marker == b"\r\n\r\n"
            return b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com\r\n\r\n"

        async def read(self, size):
            return self.chunks.pop(0) if self.chunks else b""

    class Writer:
        def __init__(self):
            self.data = bytearray()
            self.closed = False

        def write(self, data):
            self.data.extend(data)

        async def drain(self):
            pass

        def close(self):
            self.closed = True

        async def wait_closed(self):
            pass

    upstream_reader = Reader([b"reply", b""])
    upstream_writer = Writer()

    async def open_connection(host, port, **kwargs):
        connected.update(host=host, port=port, kwargs=kwargs)
        return upstream_reader, upstream_writer

    monkeypatch.setattr("app.egress_agent.validate_connect_target", validate)
    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    agent = EgressAgent("node-1", ssl_context=ssl.create_default_context(), idle_timeout_s=1)
    client_reader = Reader([b"request", b""])
    client_writer = Writer()

    await agent.handle(client_reader, client_writer)

    assert connected["host"] == "93.184.216.34"
    assert connected["port"] == 443
    assert b"X-CrawlTrove-Connected-IP: 93.184.216.34" in client_writer.data
    assert b"request" in upstream_writer.data
    assert b"reply" in client_writer.data


async def test_tunnel_stops_at_configured_byte_ceiling(monkeypatch):
    class Reader:
        def __init__(self, first):
            self.first = first

        async def readuntil(self, marker):
            return b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com\r\n\r\n"

        async def read(self, size):
            value, self.first = self.first, b""
            return value

    class Writer:
        def __init__(self):
            self.data = bytearray()

        def write(self, data):
            self.data.extend(data)

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def open_connection(*args, **kwargs):
        return Reader(b""), upstream_writer

    monkeypatch.setattr("app.egress_agent.validate_connect_target", lambda *args: asyncio.sleep(0, ("93.184.216.34",)))
    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    upstream_writer, client_writer = Writer(), Writer()
    agent = EgressAgent("node-1", ssl_context=ssl.create_default_context(), max_tunnel_bytes=5)
    await agent.handle(Reader(b"seven!!"), client_writer)
    assert upstream_writer.data == b"seven"


async def test_start_refuses_a_context_that_does_not_require_mtls():
    agent = EgressAgent("node-1", ssl_context=ssl.create_default_context())
    with pytest.raises(ValueError, match="client certificates"):
        await agent.start("127.0.0.1", 0)


def test_bundle_and_enrollment_private_files_are_required(tmp_path):
    bundle = tmp_path / "egress.json"
    bundle.write_text(json.dumps({"nodeId": "edge-1"}))
    bundle.chmod(0o644)
    with pytest.raises(ValueError, match="group/world"):
        EgressConfig.from_file(bundle)

    script_path = Path(__file__).resolve().parents[1] / "scripts" / "enroll_egress.py"
    spec = importlib.util.spec_from_file_location("enroll_egress", script_path)
    assert spec and spec.loader
    enroll = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(enroll)
    ca_key = tmp_path / "ca.key"
    ca_key.write_text("private")
    ca_key.chmod(0o644)
    with pytest.raises(ValueError, match="group/world"):
        enroll._private_file(ca_key, "CA key")
    output = tmp_path / "private-bundle.json"
    enroll._write_private(output, "{}")
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="refusing to overwrite"):
        enroll._write_private(output, "{}")


def test_tls_context_requires_client_certificates(tmp_path):
    agent = EgressAgent("node-1", ssl_context=ssl.create_default_context())
    with pytest.raises(ValueError, match="client certificates"):
        agent._validate_tls_context()


def test_configured_proxy_file_must_be_private(tmp_path):
    path = tmp_path / "proxies.json"
    path.write_text('[{"id":"corp-1","url":"https://proxy.example:9443"}]')
    path.chmod(0o644)
    with pytest.raises(PermissionError, match="0600"):
        load_configured_proxies(path)
    path.chmod(0o600)
    proxies = load_configured_proxies(path)
    assert proxies[0].id == "corp-1"
    assert proxies[0].endpoint == "https://proxy.example:9443"


def test_mtls_bridge_accepts_only_public_attested_ip():
    bridge = MtlsConnectBridge("https://edge.example:9443", ssl.create_default_context())
    response = (
        b"HTTP/1.1 200 Connection Established\r\n"
        b"X-CrawlTrove-Connected-IP: 93.184.216.34\r\n\r\n"
    )
    assert MtlsConnectBridge._connected_ip(response) == "93.184.216.34"
    with pytest.raises(ValueError, match="non-public"):
        MtlsConnectBridge._connected_ip(
            b"HTTP/1.1 200 Connection Established\r\n"
            b"X-CrawlTrove-Connected-IP: 127.0.0.1\r\n\r\n"
        )
    with pytest.raises(ValueError, match="only CONNECT"):
        bridge._connect_target(b"GET http://example.com/ HTTP/1.1\r\n\r\n")
    with pytest.raises(ValueError, match="only CONNECT"):
        bridge._connect_target(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")


@requires_db
async def test_proxy_pool_skips_unhealthy_and_cooldown_nodes(db):
    async with db.acquire() as conn:
        await conn.execute("TRUNCATE proxy_nodes CASCADE")
    pool = ProxyPool(db)
    await pool.register("a", "proxy-a:9443", regions=["sg"])
    await pool.register("b", "proxy-b:9443", regions=["sg"])
    await pool.mark_failure("a", "blocked", cooldown_seconds=300)

    lease = await pool.select("https://example.com:443", region="sg")

    assert lease is not None
    assert lease.node_id == "b"


@requires_db
async def test_proxy_pool_fences_task_lease_and_records_connected_ip(db):
    from app.crawl import repository
    from app.crawl.config import CrawlConfig

    async with db.acquire() as conn:
        await conn.execute("TRUNCATE proxy_nodes CASCADE")
    await repository.submit_job(CrawlConfig(url="https://example.com", minDelayMs=0))
    task = await repository.claim_task("proxy-test-worker", {"http", "proxy"})
    assert task is not None
    pool = ProxyPool(db)
    await pool.register("edge-a", "edge-a:9443")

    lease = await pool.select(
        task.origin_key, task_id=task.id, lease_token=task.lease_token,
    )
    assert lease is not None
    assert await pool.record_connected_ip(
        task.id, task.lease_token, lease.node_id, "93.184.216.34",
    )
    assert not await pool.record_connected_ip(
        task.id, uuid4(), lease.node_id, "93.184.216.34",
    )
    assert not await pool.mark_failure(
        lease.node_id, "blocked", task_id=task.id, lease_token=uuid4(),
    )
    async with db.acquire() as conn:
        assert await conn.fetchval(
            "SELECT proxy_id FROM acquisition_attempts WHERE task_id = $1", task.id,
        ) == "edge-a"
        assert await conn.fetchval(
            "SELECT host(last_connected_ip) FROM proxy_nodes WHERE id = 'edge-a'",
        ) == "93.184.216.34"


@requires_db
async def test_worker_proxy_api_rejects_private_connected_ip(db):
    from app.crawl import repository
    from app.crawl.config import CrawlConfig

    async with db.acquire() as conn:
        role = await conn.fetchval("SELECT session_user")
        await conn.execute("TRUNCATE proxy_nodes CASCADE")
        await conn.execute("DELETE FROM workers WHERE db_role = $1", role)
        await conn.execute(
            """INSERT INTO workers (id, db_role, capabilities, protocol_version, state,
                                      artifact_bucket, artifact_prefix)
               VALUES ('proxy-sql-test', $1, ARRAY['http','proxy'], 1, 'active',
                       'test-bucket', 'workers/proxy-sql-test/')""",
            role,
        )
    await repository.submit_job(CrawlConfig(url="https://private-guard.example", minDelayMs=0))
    task = await repository.claim_task("proxy-sql-test", {"http", "proxy"})
    assert task is not None
    await ProxyPool(db).register("sql-edge", "sql-edge:9443")
    async with db.acquire() as conn:
        assigned = await conn.fetchrow(
            "SELECT * FROM worker_api.assign_proxy($1,$2,$3)",
            task.id, task.lease_token, task.origin_key,
        )
        assert assigned is not None
        assert not await conn.fetchval(
            "SELECT worker_api.record_proxy_ip($1,$2,$3,$4::INET)",
            task.id, task.lease_token, "sql-edge", "127.0.0.1",
        )
        assert await conn.fetchval(
            "SELECT worker_api.record_proxy_ip($1,$2,$3,$4::INET)",
            task.id, task.lease_token, "sql-edge", "93.184.216.34",
        )


async def test_proxy_worker_passes_explicit_proxy_and_never_uses_environment():
    from datetime import datetime, timedelta, timezone

    task = ClaimedTask(
        id=uuid4(), job_id=uuid4(), url="https://example.com",
        normalized_url="https://example.com", origin_key="https://example.com:443",
        depth=0, attempt=1, lease_token=uuid4(),
        deadline_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        config={"url": "https://example.com", "respectRobots": False},
        byte_allowance=1024, artifact_allowance=1024,
        required_capabilities=frozenset({"proxy"}),
    )

    class Pool:
        def __init__(self):
            self.released = []

        async def select(self, *args, **kwargs):
            return ProxyLease("edge-a", "https://edge-a:9443", task.origin_key, task.id, task.lease_token)

        async def release_proxy(self, task_id, lease_token):
            self.released.append((task_id, lease_token))

        async def record_connected_ip(self, *args):
            return True

    class Repository:
        async def claim_task(self, worker_id, capabilities):
            return task

        async def heartbeat(self, task_id, lease_token):
            return True

        async def reserve_browser_navigation(self, task_id, lease_token):
            return True

        async def complete_task(self, task_id, lease_token, result):
            return True

    class Scraper:
        async def scrape(self, _url, **kwargs):
            assert kwargs["proxy"] == {"server": "https://edge-a:9443"}
            assert kwargs["trust_env"] is False
            return {"success": True, "url": task.url, "markdown": "ok",
                    "metadata": {"proxy_connected_ip": "93.184.216.34"}}

    pool = Pool()
    assert await CrawlWorker(
        "worker-a", {"http", "proxy"}, Repository(), Scraper(), proxy_pool=pool,
    ).run_once()
    assert pool.released == [(task.id, task.lease_token)]


async def test_proxy_capable_worker_keeps_ordinary_task_direct():
    from datetime import datetime, timedelta, timezone

    task = ClaimedTask(
        id=uuid4(), job_id=uuid4(), url="https://example.com",
        normalized_url="https://example.com", origin_key="https://example.com:443",
        depth=0, attempt=1, lease_token=uuid4(),
        deadline_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        config={"url": "https://example.com", "respectRobots": False},
        byte_allowance=1024, artifact_allowance=1024,
    )

    class Pool:
        async def select(self, *args, **kwargs):
            raise AssertionError("ordinary tasks must not select a proxy")

    class Repository:
        async def claim_task(self, *_): return task
        async def heartbeat(self, *_): return True
        async def reserve_browser_navigation(self, *_): return True
        async def complete_task(self, *_): return True

    class Scraper:
        async def scrape(self, _url, **kwargs):
            assert "proxy" not in kwargs and "trust_env" not in kwargs
            return {"success": True, "url": task.url, "markdown": "ok", "metadata": {}}

    assert await CrawlWorker(
        "worker-a", {"http", "proxy"}, Repository(), Scraper(), proxy_pool=Pool(),
    ).run_once()


async def test_scraper_passes_proxy_only_to_explicit_transport(monkeypatch):
    from app import fetch

    called = {}

    async def fetch_http(url, **kwargs):
        called.update(url=url, **kwargs)
        return {"status": 200, "html": "<p>ok</p>", "content": b"ok",
                "content_type": "text/html", "final_url": url}

    monkeypatch.setattr(fetch, "fetch_http", fetch_http)
    result = await WebScraper().scrape(
        "https://example.com", engine="http",
        proxy={"server": "https://edge-a:9443"}, trust_env=False,
    )
    assert result["success"] is True
    assert called["proxy"] == "https://edge-a:9443"


async def test_proxy_worker_rejects_ambient_proxy_settings():
    with pytest.raises(ValueError, match="environment proxy"):
        await WebScraper().scrape("https://example.com", engine="http", trust_env=True)


async def test_proxy_http_session_disables_environment_inheritance(monkeypatch):
    from app.fetch import HttpFetcher

    created = []

    class Session:
        def __init__(self, **kwargs):
            created.append(kwargs)
            self.curl_options = {}

    fetcher = HttpFetcher(
        session_factory=Session, proxy="https://edge-a:9443",
    )
    await fetcher.start()
    await fetcher.close()
    assert created == [{
        "impersonate": "chrome", "trust_env": False,
        "proxy": "https://edge-a:9443",
    }]


async def test_proxy_http_session_keeps_credentials_out_of_url():
    from app.fetch import HttpFetcher

    created = []

    class Session:
        def __init__(self, **kwargs):
            created.append(kwargs)
            self.curl_options = {}

    fetcher = HttpFetcher(
        session_factory=Session, proxy="https://edge-a:9443", proxy_auth=("user", "secret"),
    )
    await fetcher.start()
    await fetcher.close()
    assert created[0]["proxy"] == "https://edge-a:9443"
    assert created[0]["proxy_auth"] == ("user", "secret")


async def test_mtls_bridge_attests_before_opening_local_tunnel(monkeypatch):
    class UpstreamReader:
        def __init__(self):
            self.header = True

        async def readuntil(self, _marker):
            assert self.header
            self.header = False
            return (b"HTTP/1.1 200 Connection Established\r\n"
                    b"X-CrawlTrove-Connected-IP: 93.184.216.34\r\n\r\n")

        async def read(self, _size):
            return b""

    class UpstreamWriter:
        def __init__(self): self.data = bytearray()
        def write(self, data): self.data.extend(data)
        async def drain(self): pass
        def close(self): pass
        async def wait_closed(self): pass

    bridge = MtlsConnectBridge("https://edge.example:9443", ssl.create_default_context())
    upstream = UpstreamWriter()

    async def open_upstream():
        return UpstreamReader(), upstream

    monkeypatch.setattr(bridge, "_open_upstream", open_upstream)
    attested = []

    async def attest(address):
        attested.append(address)
        return True

    await bridge.start(attest)
    parsed = bridge.server_url.split(":")
    reader, writer = await asyncio.open_connection(parsed[1].lstrip("/"), int(parsed[2]))
    username, password = bridge.local_credentials
    auth = base64.b64encode((username + ":" + password).encode()).decode()
    writer.write(("CONNECT example.com:443 HTTP/1.1\r\nHost: example.com\r\n"
                  "Proxy-Authorization: Basic %s\r\n\r\n" % auth).encode())
    await writer.drain()
    assert b"200 Connection Established" in await reader.read()
    writer.close()
    await writer.wait_closed()
    await bridge.close()
    assert attested == ["93.184.216.34"]
    assert upstream.data.startswith(b"CONNECT example.com:443")
    assert b"Authorization:" not in upstream.data
