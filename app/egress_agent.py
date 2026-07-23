"""A narrowly scoped, mutually authenticated HTTP CONNECT egress agent."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import ipaddress
import json
import logging
import re
import socket
import ssl
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


MAX_CONNECT_HEADER_BYTES = 16 * 1024
TUNNEL_BUFFER_BYTES = 64 * 1024
DEFAULT_TUNNEL_BYTES = 100 * 1024 * 1024
DEFAULT_IDLE_TIMEOUT_S = 60.0
VALID_PORTS = frozenset({80, 443})
NODE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_FORWARDED_HEADERS = {
    "forwarded", "proxy-authorization", "proxy-connection", "via",
    "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-real-ip",
}


class ConnectError(ValueError):
    """A malformed or unsafe CONNECT request."""


def _private_file(path: Path, label: str) -> None:
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise ValueError(f"{label} does not exist: {path}") from exc
    if mode & 0o077:
        raise ValueError(f"{label} must not be group/world-readable: {path}")


def _normalise_host(host: str) -> str:
    if not host or len(host) > 253 or "%" in host:
        raise ConnectError("CONNECT authority must contain one hostname")
    if any(marker in host for marker in ("/", "?", "#", "@")):
        raise ConnectError("CONNECT authority must not include URL components")
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        encoded = host.encode("ascii").decode("ascii")
        # Decode only to validate an IDNA label; keep the ASCII form for DNS.
        encoded.encode("ascii").decode("idna")
    except (UnicodeError, UnicodeDecodeError) as exc:
        raise ConnectError("CONNECT hostname must be ASCII or IDNA") from exc
    if encoded.endswith(".") or any(
        not HOST_LABEL.fullmatch(label) for label in encoded.split(".")
    ):
        raise ConnectError("CONNECT hostname is malformed")
    return encoded.lower()


def _parse_authority(authority: str, allowed_ports: Iterable[int]) -> tuple[str, int]:
    if authority.startswith("["):
        closing = authority.find("]")
        if closing <= 1 or authority[closing + 1:closing + 2] != ":":
            raise ConnectError("CONNECT IPv6 authority is malformed")
        if authority.find("]", closing + 1) != -1:
            raise ConnectError("CONNECT IPv6 authority is malformed")
        host, port_text = authority[1:closing], authority[closing + 2:]
        try:
            if ipaddress.ip_address(host).version != 6:
                raise ValueError
        except ValueError as exc:
            raise ConnectError("CONNECT IPv6 authority is malformed") from exc
    else:
        if authority.count(":") != 1:
            raise ConnectError("CONNECT authority must include host and port")
        host, port_text = authority.rsplit(":", 1)
    if not port_text.isascii() or not port_text.isdecimal():
        raise ConnectError("CONNECT port is malformed")
    port = int(port_text)
    if port not in set(allowed_ports):
        raise ConnectError("CONNECT port is not allowed")
    return _normalise_host(host), port


def _parse_host_header(authority: str, allowed_ports: Iterable[int],
                       default_port: int) -> tuple[str, int]:
    if authority.startswith("[") and authority.endswith("]"):
        host = authority[1:-1]
        try:
            if ipaddress.ip_address(host).version != 6:
                raise ValueError
        except ValueError as exc:
            raise ConnectError("CONNECT Host header is malformed") from exc
        return _normalise_host(host), default_port
    if ":" not in authority:
        return _normalise_host(authority), default_port
    return _parse_authority(authority, allowed_ports)


def parse_connect(request: bytes, allowed_ports: Iterable[int] = VALID_PORTS) -> tuple[str, int]:
    """Parse exactly one bounded HTTP/1.1 CONNECT request."""
    allowed_ports = frozenset(allowed_ports)
    if len(request) > MAX_CONNECT_HEADER_BYTES or not request.endswith(b"\r\n\r\n"):
        raise ConnectError("CONNECT headers are malformed or too large")
    try:
        header_block = request[:-4].decode("ascii")
    except UnicodeDecodeError as exc:
        raise ConnectError("CONNECT headers must be ASCII") from exc
    lines = header_block.split("\r\n")
    if not lines or not lines[0]:
        raise ConnectError("CONNECT request line is missing")
    parts = lines[0].split(" ")
    if len(parts) != 3 or parts[0] != "CONNECT" or parts[2] != "HTTP/1.1":
        raise ConnectError("only HTTP/1.1 CONNECT is supported")
    host, port = _parse_authority(parts[1], allowed_ports)
    host_headers: list[str] = []
    for line in lines[1:]:
        if not line or ":" not in line:
            raise ConnectError("CONNECT header is malformed")
        name, value = line.split(":", 1)
        if not name or not name.isascii():
            raise ConnectError("CONNECT header is malformed")
        lowered = name.lower()
        if lowered in _FORWARDED_HEADERS or lowered.startswith("x-forwarded-"):
            raise ConnectError("CONNECT forwarding headers are not allowed")
        if lowered in {"content-length", "transfer-encoding"}:
            raise ConnectError("CONNECT request bodies are not allowed")
        if lowered == "host":
            host_headers.append(value.strip())
    if len(host_headers) != 1:
        raise ConnectError("CONNECT requires exactly one Host header")
    header_host, header_port = _parse_host_header(host_headers[0], allowed_ports, port)
    if (header_host, header_port) != (host, port):
        raise ConnectError("CONNECT Host header does not match authority")
    return host, port


def validate_addresses(addresses: Iterable[str]) -> tuple[str, ...]:
    """Return validated global addresses, rejecting mixed DNS answers."""
    result: list[str] = []
    for raw in addresses:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise ConnectError("DNS answer is not an IP address") from exc
        if not address.is_global or address.is_multicast or address.is_unspecified:
            raise ConnectError("CONNECT target must resolve only to public addresses")
        text = str(address)
        if text not in result:
            result.append(text)
    if not result:
        raise ConnectError("CONNECT target must resolve to public addresses")
    return tuple(result)


async def validate_connect_target(host: str, port: int) -> tuple[str, ...]:
    """Resolve once and fail closed if any DNS candidate is non-public."""
    host = _normalise_host(host)
    try:
        records = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise ConnectError("CONNECT target could not be resolved") from exc
    return validate_addresses(record[4][0] for record in records)


def create_server_ssl_context(ca_cert: str | Path, node_cert: str | Path,
                              node_key: str | Path, crl: str | Path | None = None) -> ssl.SSLContext:
    """Build the fail-closed server context used by the mTLS listener."""
    if not all(isinstance(value, (str, Path)) and str(value) for value in (ca_cert, node_cert, node_key)):
        raise ValueError("egress CA, node certificate, and node key are required")
    if crl is not None and (not isinstance(crl, (str, Path)) or not str(crl)):
        raise ValueError("egress CRL path is invalid")
    ca_path, cert_path, key_path = Path(ca_cert), Path(node_cert), Path(node_key)
    _private_file(key_path, "egress node key")
    if not ca_path.is_file() or not cert_path.is_file():
        raise ValueError("egress CA and node certificate files are required")
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
        context.load_verify_locations(cafile=str(ca_path))
        if crl is not None:
            crl_path = Path(crl)
            if not crl_path.is_file():
                raise ValueError("egress CRL file is required")
            context.load_verify_locations(cafile=str(crl_path))
            context.verify_flags |= ssl.VERIFY_CRL_CHECK_LEAF
    except (OSError, ssl.SSLError) as exc:
        raise ValueError("egress TLS configuration is invalid") from exc
    return context


@dataclass(frozen=True)
class EgressConfig:
    node_id: str
    ssl_context: ssl.SSLContext
    allowed_ports: frozenset[int]
    listen_host: str = "0.0.0.0"
    listen_port: int = 9443
    max_tunnel_bytes: int = DEFAULT_TUNNEL_BYTES
    idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S

    @classmethod
    def from_file(cls, path: str | Path) -> "EgressConfig":
        bundle = Path(path)
        _private_file(bundle, "egress enrollment bundle")
        try:
            data = json.loads(bundle.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid egress enrollment bundle: {bundle}") from exc
        if not isinstance(data, dict):
            raise ValueError("egress enrollment bundle must be a JSON object")
        node_id = data.get("nodeId")
        if not isinstance(node_id, str) or not NODE_ID.fullmatch(node_id):
            raise ValueError("nodeId must contain only letters, digits, underscores, and hyphens")
        ports = data.get("allowedPorts", sorted(VALID_PORTS))
        if not isinstance(ports, list) or not ports or any(port not in VALID_PORTS for port in ports):
            raise ValueError("allowedPorts may contain only 80 and 443")
        return cls(
            node_id=node_id,
            ssl_context=create_server_ssl_context(
                data.get("caCert", ""), data.get("nodeCert", ""), data.get("nodeKey", ""),
                data.get("crl"),
            ),
            allowed_ports=frozenset(ports),
            listen_host=str(data.get("listenHost", "0.0.0.0")),
            listen_port=int(data.get("listenPort", 9443)),
            max_tunnel_bytes=int(data.get("maxTunnelBytes", DEFAULT_TUNNEL_BYTES)),
            idle_timeout_s=float(data.get("idleTimeoutSeconds", DEFAULT_IDLE_TIMEOUT_S)),
        )


class _ByteBudget:
    def __init__(self, ceiling: int):
        self.ceiling = ceiling
        self.used = 0
        self._lock = asyncio.Lock()

    async def consume(self, size: int) -> int:
        async with self._lock:
            allowed = min(size, max(0, self.ceiling - self.used))
            self.used += allowed
            return allowed


class EgressAgent:
    """mTLS-only CONNECT service that pins outbound TCP to a checked address."""

    def __init__(self, node_id: str, *, ssl_context: ssl.SSLContext,
                 allowed_ports: Iterable[int] = VALID_PORTS,
                 max_tunnel_bytes: int = DEFAULT_TUNNEL_BYTES,
                 idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S,
                 logger: logging.Logger | None = None):
        if not node_id:
            raise ValueError("node_id is required")
        if max_tunnel_bytes <= 0 or idle_timeout_s <= 0:
            raise ValueError("tunnel limits must be positive")
        self.node_id = node_id
        self.ssl_context = ssl_context
        self.allowed_ports = frozenset(allowed_ports)
        if not self.allowed_ports or not self.allowed_ports <= VALID_PORTS:
            raise ValueError("allowed ports may contain only 80 and 443")
        self.max_tunnel_bytes = max_tunnel_bytes
        self.idle_timeout_s = idle_timeout_s
        self._logger = logger or logging.getLogger(__name__)
        self._server: asyncio.AbstractServer | None = None

    @classmethod
    def from_config(cls, config: EgressConfig) -> "EgressAgent":
        return cls(config.node_id, ssl_context=config.ssl_context, allowed_ports=config.allowed_ports,
                   max_tunnel_bytes=config.max_tunnel_bytes, idle_timeout_s=config.idle_timeout_s)

    def _validate_tls_context(self) -> None:
        if self.ssl_context.verify_mode != ssl.CERT_REQUIRED or self.ssl_context.check_hostname:
            raise ValueError("egress TLS context must require client certificates")

    async def start(self, host: str = "0.0.0.0", port: int = 9443) -> asyncio.AbstractServer:
        self._validate_tls_context()
        self._server = await asyncio.start_server(
            self.handle, host, port, ssl=self.ssl_context, limit=MAX_CONNECT_HEADER_BYTES,
        )
        return self._server

    async def serve(self, host: str = "0.0.0.0", port: int = 9443) -> None:
        server = await self.start(host, port)
        async with server:
            await server.serve_forever()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def handle(self, client_reader: asyncio.StreamReader,
                     client_writer: asyncio.StreamWriter) -> None:
        started = time.monotonic()
        host_hash = ""
        connected_ip = ""
        budget = _ByteBudget(self.max_tunnel_bytes)
        upstream_writer = None
        try:
            try:
                request = await client_reader.readuntil(b"\r\n\r\n")
                host, port = parse_connect(request, self.allowed_ports)
                host_hash = hashlib.sha256(host.encode("ascii")).hexdigest()[:16]
                addresses = await validate_connect_target(host, port)
                connected_ip = addresses[0]
                family = socket.AF_INET6 if ":" in connected_ip else socket.AF_INET
                upstream_reader, upstream_writer = await asyncio.open_connection(
                    connected_ip, port, family=family,
                )
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectError) as exc:
                await self._respond(client_writer, 400, "Bad Request")
                self._logger.warning("egress request rejected node=%s reason=%s", self.node_id, type(exc).__name__)
                return
            except (OSError, asyncio.TimeoutError):
                await self._respond(client_writer, 502, "Bad Gateway")
                return
            client_writer.write(
                b"HTTP/1.1 200 Connection Established\r\n"
                + f"X-CrawlTrove-Connected-IP: {connected_ip}\r\n\r\n".encode("ascii")
            )
            await client_writer.drain()
            await asyncio.gather(
                self._pump(client_reader, upstream_writer, budget),
                self._pump(upstream_reader, client_writer, budget),
            )
        finally:
            await self._close_writer(upstream_writer)
            await self._close_writer(client_writer)
            self._logger.info(
                "egress tunnel node=%s destination_hash=%s connected_ip=%s bytes=%d duration_ms=%d",
                self.node_id, host_hash, connected_ip, budget.used,
                int((time.monotonic() - started) * 1000),
            )

    async def _pump(self, source: asyncio.StreamReader, destination: asyncio.StreamWriter,
                    budget: _ByteBudget) -> None:
        while budget.used < budget.ceiling:
            try:
                chunk = await asyncio.wait_for(source.read(TUNNEL_BUFFER_BYTES), self.idle_timeout_s)
            except asyncio.TimeoutError:
                return
            if not chunk:
                try:
                    destination.write_eof()
                    await destination.drain()
                except (AttributeError, OSError):
                    pass
                return
            allowed = await budget.consume(len(chunk))
            if not allowed:
                return
            destination.write(chunk[:allowed])
            await destination.drain()
            if allowed < len(chunk):
                return

    @staticmethod
    async def _respond(writer: asyncio.StreamWriter, status: int, reason: str) -> None:
        writer.write(f"HTTP/1.1 {status} {reason}\r\nConnection: close\r\n\r\n".encode("ascii"))
        try:
            await writer.drain()
        except OSError:
            pass

    @staticmethod
    async def _close_writer(writer: asyncio.StreamWriter | None) -> None:
        if writer is None:
            return
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, ConnectionError):
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True)
    return parser


async def _main_async(bundle: str) -> None:
    config = EgressConfig.from_file(bundle)
    await EgressAgent.from_config(config).serve(config.listen_host, config.listen_port)


def main() -> int:
    args = _parser().parse_args()
    try:
        asyncio.run(_main_async(args.bundle))
    except (ValueError, OSError) as exc:
        logging.getLogger(__name__).error("egress agent failed: %s", exc)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
