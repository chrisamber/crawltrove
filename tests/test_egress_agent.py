import asyncio
import importlib.util
import json
from pathlib import Path
import ssl

import pytest

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
