"""Operator-owned HTTP CONNECT proxy registration and fenced selection."""
from __future__ import annotations

import asyncio
import base64
import hmac
import ipaddress
import json
import os
import re
import secrets
import stat
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit
from uuid import UUID


_NODE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_REGION = re.compile(r"[a-z0-9][a-z0-9-]{0,31}\Z")
_SECRET_KEYS = ("usernameFile", "passwordFile", "clientCertFile", "clientKeyFile", "caFile")


@dataclass(frozen=True)
class ConfiguredProxy:
    """One operator configuration entry; paths are retained, secrets are not."""

    id: str
    endpoint: str
    region: str | None = None
    username_file: Path | None = None
    password_file: Path | None = None
    client_cert_file: Path | None = None
    client_key_file: Path | None = None
    ca_file: Path | None = None

    def credentials(self) -> tuple[str, str] | None:
        if self.username_file is None:
            return None
        _require_private_regular_file(self.username_file, "proxy usernameFile", exact_mode=False)
        _require_private_regular_file(self.password_file, "proxy passwordFile", exact_mode=False)
        username = self.username_file.read_text(encoding="utf-8").rstrip("\r\n")
        password = self.password_file.read_text(encoding="utf-8").rstrip("\r\n")
        if not username or not password:
            raise ValueError("proxy credentials must not be empty")
        return username, password

    def tls_context(self) -> ssl.SSLContext | None:
        if self.client_cert_file is None:
            return None
        for path, label in ((self.client_cert_file, "proxy clientCertFile"),
                            (self.client_key_file, "proxy clientKeyFile"),
                            (self.ca_file, "proxy caFile")):
            _require_private_regular_file(path, label, exact_mode=False)
        context = ssl.create_default_context(cafile=str(self.ca_file))
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_cert_chain(str(self.client_cert_file), str(self.client_key_file))
        return context


@dataclass(frozen=True)
class ProxyLease:
    node_id: str
    endpoint: str
    origin_key: str
    task_id: UUID | None = None
    lease_token: UUID | None = None
    username: str | None = None
    password: str | None = None
    bridge: "MtlsConnectBridge | None" = None

    def playwright_proxy(self) -> dict[str, str]:
        """Explicit browser proxy settings; ambient proxy settings stay disabled."""
        server = self.bridge.server_url if self.bridge is not None else self.endpoint
        server = server if "://" in server else "https://" + server
        result = {"server": server}
        credentials = self.bridge.local_credentials if self.bridge is not None else (
            (self.username, self.password) if self.username is not None and self.password is not None else None
        )
        if credentials is not None:
            result.update(username=credentials[0], password=credentials[1])
        return result

    async def start(self, attest: Any) -> None:
        if self.bridge is not None:
            await self.bridge.start(attest)

    async def close(self) -> None:
        if self.bridge is not None:
            await self.bridge.close()


class MtlsConnectBridge:
    """One worker-local HTTP CONNECT bridge to an mTLS egress agent."""

    def __init__(self, endpoint: str, context: ssl.SSLContext) -> None:
        parsed = urlsplit(endpoint)
        self._host = parsed.hostname
        self._port = parsed.port
        if not self._host or not self._port:
            raise ValueError("mTLS proxy endpoint is invalid")
        self._context = context
        self._server: asyncio.AbstractServer | None = None
        self._attest: Any = None
        self._local_username = secrets.token_urlsafe(18)
        self._local_password = secrets.token_urlsafe(24)
        self._handlers: set[asyncio.Task] = set()

    @property
    def local_credentials(self) -> tuple[str, str]:
        return self._local_username, self._local_password

    @property
    def server_url(self) -> str:
        if self._server is None or not self._server.sockets:
            raise RuntimeError("mTLS proxy bridge is not running")
        return "http://127.0.0.1:%d" % self._server.sockets[0].getsockname()[1]

    async def start(self, attest: Any) -> None:
        self._attest = attest
        if self._server is None:
            self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0, limit=16 * 1024)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        handlers, self._handlers = self._handlers, set()
        for task in handlers:
            task.cancel()
        if handlers:
            await asyncio.gather(*handlers, return_exceptions=True)

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(self._handle(reader, writer))
        self._handlers.add(task)
        try:
            await task
        finally:
            self._handlers.discard(task)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream_writer = None
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
            host, port, upstream_request = self._connect_target(request)
            upstream_reader, upstream_writer = await asyncio.wait_for(self._open_upstream(), timeout=10)
            upstream_writer.write(upstream_request)
            await upstream_writer.drain()
            response = await asyncio.wait_for(upstream_reader.readuntil(b"\r\n\r\n"), timeout=10)
            address = self._connected_ip(response)
            if self._attest is None or not await self._attest(address):
                raise ValueError("egress connected IP was rejected")
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await self._pump(reader, upstream_writer, upstream_reader, writer)
        except Exception:
            if not writer.is_closing():
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
                await writer.drain()
        finally:
            if upstream_writer is not None:
                upstream_writer.close()
                await upstream_writer.wait_closed()
            writer.close()
            await writer.wait_closed()

    def _connect_target(self, request: bytes) -> tuple[str, int, bytes]:
        try:
            lines = request.decode("ascii", "strict").split("\r\n")
            line = lines[0]
            method, authority, version = line.split(" ")
            parsed = urlsplit("//" + authority)
            expected = "Basic " + base64.b64encode(
                (self._local_username + ":" + self._local_password).encode("ascii")
            ).decode("ascii")
            supplied = [line.split(":", 1)[1].strip() for line in lines[1:]
                        if line.lower().startswith("proxy-authorization:")]
            if (method != "CONNECT" or version != "HTTP/1.1" or not parsed.hostname
                    or parsed.port is None or parsed.username or parsed.password
                    or parsed.path not in {"", "/"} or len(supplied) != 1
                    or not hmac.compare_digest(supplied[0], expected)):
                raise ValueError
            rebuilt = ("CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n" % (authority, authority)).encode("ascii")
            return parsed.hostname, parsed.port, rebuilt
        except (UnicodeDecodeError, ValueError):
            raise ValueError("local bridge accepts only CONNECT authorities") from None

    async def _open_upstream(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_connection(
            self._host, self._port, ssl=self._context, server_hostname=self._host,
            limit=16 * 1024,
        )

    @staticmethod
    def _connected_ip(response: bytes) -> str:
        lines = response.decode("ascii", "strict").split("\r\n")
        if not lines or not lines[0].startswith("HTTP/1.1 200 "):
            raise ValueError("egress CONNECT was refused")
        values = [line.split(":", 1)[1].strip() for line in lines[1:]
                  if line.lower().startswith("x-crawltrove-connected-ip:")]
        if len(values) != 1:
            raise ValueError("egress CONNECT did not attest one connected IP")
        address = ipaddress.ip_address(values[0])
        if not address.is_global or address.is_multicast or address.is_unspecified:
            raise ValueError("egress CONNECT attested a non-public IP")
        return str(address)

    @staticmethod
    async def _pump(first_reader: asyncio.StreamReader, first_writer: asyncio.StreamWriter,
                    second_reader: asyncio.StreamReader, second_writer: asyncio.StreamWriter) -> None:
        async def copy(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
            while chunk := await source.read(64 * 1024):
                sink.write(chunk)
                await sink.drain()
        forward = asyncio.create_task(copy(first_reader, first_writer))
        reverse = asyncio.create_task(copy(second_reader, second_writer))
        done, pending = await asyncio.wait({forward, reverse}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*done, *pending, return_exceptions=True)


def _require_private_regular_file(path: Path, label: str, *, exact_mode: bool) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError(f"{label} does not exist: {path}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{label} must be a regular file: {path}")
    if info.st_uid != os.geteuid():
        raise PermissionError(f"{label} must be owned by the service user")
    mode = stat.S_IMODE(info.st_mode)
    if (exact_mode and mode != 0o600) or (not exact_mode and mode & 0o077):
        raise PermissionError(f"{label} must have mode 0600")


def _endpoint(value: object, *, configured: bool) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("proxy endpoint is required")
    parsed = urlsplit(value if "://" in value else "//" + value)
    if configured and parsed.scheme != "https":
        raise ValueError("configured proxy URL must use https")
    if not configured and parsed.scheme not in {"", "https"}:
        raise ValueError("owned proxy endpoint must use https")
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("proxy endpoint must be an authority without credentials")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("proxy endpoint port is invalid") from exc
    if port is None or not 1 <= port <= 65535 or parsed.path not in {"", "/"}:
        raise ValueError("proxy endpoint must include one valid port")
    return "https://" + parsed.netloc


def _regions(values: Sequence[str] | None) -> list[str]:
    result = list(values or ())
    if len(set(result)) != len(result) or any(
        not isinstance(value, str) or not _REGION.fullmatch(value) for value in result
    ):
        raise ValueError("proxy regions must be unique lowercase region labels")
    return result


def load_configured_proxies(path: str | os.PathLike[str]) -> tuple[ConfiguredProxy, ...]:
    """Load only a service-owned mode-0600 proxy configuration file."""
    config_path = Path(path)
    _require_private_regular_file(config_path, "PROXY_POOLS_FILE", exact_mode=True)
    try:
        entries = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("PROXY_POOLS_FILE must contain a JSON list") from exc
    if not isinstance(entries, list):
        raise ValueError("PROXY_POOLS_FILE must contain a JSON list")
    configured: list[ConfiguredProxy] = []
    ids: set[str] = set()
    allowed = {"id", "url", "region", *_SECRET_KEYS}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - allowed:
            raise ValueError("proxy configuration contains unsupported fields")
        node_id = entry.get("id")
        if not isinstance(node_id, str) or not _NODE_ID.fullmatch(node_id) or node_id in ids:
            raise ValueError("proxy configuration IDs must be unique stable IDs")
        region = entry.get("region")
        if region is not None and (not isinstance(region, str) or not _REGION.fullmatch(region)):
            raise ValueError("proxy region is invalid")
        username, password = entry.get("usernameFile"), entry.get("passwordFile")
        if (username is None) != (password is None):
            raise ValueError("proxy username and password files must be configured together")
        mtls_values = (entry.get("clientCertFile"), entry.get("clientKeyFile"), entry.get("caFile"))
        if any(value is not None for value in mtls_values) and not all(mtls_values):
            raise ValueError("proxy mTLS certificate, key, and CA files are required together")
        files: dict[str, Path] = {}
        for key in _SECRET_KEYS:
            value = entry.get(key)
            if value is None:
                continue
            if not isinstance(value, str) or not value:
                raise ValueError(f"proxy {key} must reference a mounted secret file")
            secret = Path(value)
            _require_private_regular_file(secret, f"proxy {key}", exact_mode=False)
            files[key] = secret
        configured.append(ConfiguredProxy(
            node_id, _endpoint(entry.get("url"), configured=True), region,
            files.get("usernameFile"), files.get("passwordFile"),
            files.get("clientCertFile"), files.get("clientKeyFile"), files.get("caFile"),
        ))
        ids.add(node_id)
    return tuple(configured)


class ProxyPool:
    """Select a healthy enrolled node without accepting job-supplied endpoints."""

    def __init__(self, pool: Any, configured: Sequence[ConfiguredProxy] = ()) -> None:
        self._pool = pool
        self._configured = tuple(configured)
        self._configured_by_id = {entry.id: entry for entry in self._configured}

    @classmethod
    def from_environment(cls, pool: Any) -> "ProxyPool":
        path = os.environ.get("PROXY_POOLS_FILE", "").strip()
        return cls(pool, load_configured_proxies(path) if path else ())

    async def _merge_configured(self, conn: Any) -> None:
        for configured in self._configured:
            regions = [configured.region] if configured.region else []
            await conn.execute(
                """INSERT INTO proxy_nodes
                   (id, endpoint, regions, state, credential_expires_at)
                   VALUES ($1, $2, $3::TEXT[], 'active', 'infinity')
                   ON CONFLICT (id) DO UPDATE SET endpoint = EXCLUDED.endpoint,
                       regions = EXCLUDED.regions
                   WHERE proxy_nodes.state <> 'revoked'""",
                configured.id, configured.endpoint, regions,
            )

    async def register(
        self,
        node_id: str,
        endpoint: str,
        *,
        regions: Sequence[str] | None = None,
        credential_expires_at: datetime | None = None,
    ) -> None:
        if not isinstance(node_id, str) or not _NODE_ID.fullmatch(node_id):
            raise ValueError("proxy node ID is invalid")
        expires_at = credential_expires_at or (datetime.now(timezone.utc) + timedelta(days=1))
        if expires_at.tzinfo is None or expires_at <= datetime.now(timezone.utc):
            raise ValueError("proxy credentials must expire in the future")
        async with self._pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO proxy_nodes
                   (id, endpoint, regions, state, credential_expires_at, last_seen_at)
                   VALUES ($1, $2, $3::TEXT[], 'active', $4, now())
                   ON CONFLICT (id) DO UPDATE SET endpoint = EXCLUDED.endpoint,
                       regions = EXCLUDED.regions,
                       credential_expires_at = EXCLUDED.credential_expires_at,
                       state = CASE WHEN proxy_nodes.state = 'revoked'
                                    THEN 'revoked' ELSE 'active' END,
                       last_seen_at = now()""",
                node_id, _endpoint(endpoint, configured=False), _regions(regions), expires_at,
            )

    async def heartbeat(self, node_id: str) -> bool:
        async with self._pool.acquire() as conn:
            return bool(await conn.fetchval(
                """UPDATE proxy_nodes SET last_seen_at = now()
                   WHERE id = $1 AND state = 'active' AND credential_expires_at > now()
                   RETURNING TRUE""",
                node_id,
            ))

    async def mark_failure(
        self, node_id: str, outcome: str, *, cooldown_seconds: int = 300,
        offline_after: int = 3, task_id: UUID | None = None,
        lease_token: UUID | None = None,
    ) -> bool:
        if cooldown_seconds < 1 or offline_after < 1:
            raise ValueError("proxy failure thresholds must be positive")
        async with self._pool.acquire() as conn:
            fence = ""
            if task_id is not None or lease_token is not None:
                if task_id is None or lease_token is None:
                    raise ValueError("proxy failure fence requires task and lease token")
                fence = """ AND EXISTS (
                    SELECT 1 FROM proxy_leases l JOIN crawl_tasks t ON t.id = l.task_id
                    WHERE l.node_id = proxy_nodes.id AND l.task_id = $3 AND l.lease_token = $4
                      AND l.expires_at > now() AND t.state = 'leased' AND t.lease_token = $4
                )"""
            if outcome == "blocked":
                values = (node_id, cooldown_seconds, task_id, lease_token) if fence else (node_id, cooldown_seconds)
                return bool(await conn.fetchval(
                    """UPDATE proxy_nodes SET state = 'cooldown',
                           cooldown_until = now() + ($2 * interval '1 second'),
                           failure_count = failure_count + 1
                       WHERE id = $1 AND state <> 'revoked'""" + fence + " RETURNING TRUE",
                    *values,
                ))
            if outcome != "transport":
                raise ValueError("proxy failure outcome must be blocked or transport")
            values = (node_id, offline_after, task_id, lease_token) if fence else (node_id, offline_after)
            return bool(await conn.fetchval(
                   """UPDATE proxy_nodes SET failure_count = failure_count + 1,
                       state = CASE WHEN failure_count + 1 >= $2 THEN 'offline' ELSE state END
                   WHERE id = $1 AND state <> 'revoked'""" + fence + " RETURNING TRUE",
                *values,
            ))

    async def select(
        self,
        origin_key: str,
        *,
        region: str | None = None,
        task_id: UUID | None = None,
        lease_token: UUID | None = None,
    ) -> ProxyLease | None:
        if not isinstance(origin_key, str) or not origin_key:
            raise ValueError("proxy selection requires an origin key")
        if region is not None:
            _regions([region])
        if (task_id is None) != (lease_token is None):
            raise ValueError("proxy task and lease token must be supplied together")
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await self._merge_configured(conn)
                await conn.execute("DELETE FROM proxy_leases WHERE expires_at <= now()")
                if task_id is not None:
                    task = await conn.fetchrow(
                        """SELECT id, attempt_count FROM crawl_tasks WHERE id = $1 AND state = 'leased'
                           AND lease_token = $2 FOR UPDATE""",
                        task_id, lease_token,
                    )
                    if task is None:
                        return None
                node = await conn.fetchrow(
                    """SELECT n.id, n.endpoint,
                              (SELECT count(*) FROM proxy_leases l
                               WHERE l.node_id = n.id AND l.expires_at > now()) AS active_leases
                       FROM proxy_nodes n
                       WHERE n.state = 'active' AND n.credential_expires_at > now()
                         AND (n.cooldown_until IS NULL OR n.cooldown_until <= now())
                         AND ($1::TEXT IS NULL OR $1 = ANY(n.regions))
                       ORDER BY active_leases, n.id
                       FOR UPDATE OF n SKIP LOCKED LIMIT 1""",
                    region,
                )
                if node is None:
                    return None
                if task_id is not None:
                    await conn.execute(
                        """INSERT INTO proxy_leases (node_id, task_id, origin_key, lease_token, expires_at)
                           VALUES ($1, $2, $3, $4, now() + interval '120 seconds')
                           ON CONFLICT (task_id) DO NOTHING""",
                        node["id"], task_id, origin_key, lease_token,
                    )
                    assigned = await conn.fetchval(
                        """UPDATE acquisition_attempts SET proxy_id = $3
                           WHERE task_id = $1 AND attempt_number = $2 AND finished_at IS NULL
                           RETURNING id""",
                        task_id, task["attempt_count"], node["id"],
                    )
                    if assigned is None:
                        await conn.execute(
                            "DELETE FROM proxy_leases WHERE task_id = $1 AND lease_token = $2",
                            task_id, lease_token,
                        )
                        return None
                configured = self._configured_by_id.get(node["id"])
                credentials = configured.credentials() if configured else None
                tls_context = configured.tls_context() if configured else None
                bridge = MtlsConnectBridge(node["endpoint"], tls_context) if tls_context else None
                return ProxyLease(
                    node["id"], node["endpoint"], origin_key, task_id, lease_token,
                    *(credentials or (None, None)), bridge,
                )

    async def record_connected_ip(
        self, task_id: UUID, lease_token: UUID, node_id: str, address: str,
    ) -> bool:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            return False
        if not parsed.is_global or parsed.is_multicast or parsed.is_unspecified:
            return False
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                matched = await conn.fetchval(
                    """UPDATE proxy_nodes n SET last_connected_ip = $4::INET, last_seen_at = now()
                       FROM proxy_leases l JOIN crawl_tasks t ON t.id = l.task_id
                       WHERE n.id = $3 AND l.node_id = n.id AND l.task_id = $1
                         AND l.lease_token = $2 AND l.expires_at > now()
                         AND t.state = 'leased' AND t.lease_token = $2
                       RETURNING TRUE""",
                    task_id, lease_token, node_id, str(parsed),
                )
                if not matched:
                    return False
                await conn.execute(
                    """UPDATE acquisition_attempts SET proxy_id = $2,
                           proxy_connected_ip = $3::INET
                       WHERE task_id = $1
                         AND attempt_number = (SELECT attempt_count FROM crawl_tasks WHERE id = $1)
                         AND finished_at IS NULL""",
                    task_id, node_id, str(parsed),
                )
                return True

    async def release_proxy(self, task_id: UUID, lease_token: UUID) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM proxy_leases WHERE task_id = $1 AND lease_token = $2",
                task_id, lease_token,
            )


class RemoteProxyPool:
    """Fenced remote-worker facade; it never receives table access or secrets."""

    def __init__(self, repository: Any, configured: Sequence[ConfiguredProxy] = ()) -> None:
        self._repository = repository
        self._configured_by_id = {entry.id: entry for entry in configured}

    @classmethod
    def from_environment(cls, repository: Any) -> "RemoteProxyPool":
        path = os.environ.get("PROXY_POOLS_FILE", "").strip()
        return cls(repository, load_configured_proxies(path) if path else ())

    async def select(self, origin_key: str, *, task_id: UUID,
                     lease_token: UUID) -> ProxyLease | None:
        row = await self._repository.assign_proxy(task_id, lease_token, origin_key)
        if row is None:
            return None
        configured = self._configured_by_id.get(row["node_id"])
        if configured is not None and configured.endpoint != row["endpoint"]:
            return None
        credentials = configured.credentials() if configured else None
        tls_context = configured.tls_context() if configured else None
        bridge = MtlsConnectBridge(row["endpoint"], tls_context) if tls_context else None
        return ProxyLease(
            row["node_id"], row["endpoint"], origin_key, task_id, lease_token,
            *(credentials or (None, None)), bridge,
        )

    async def record_connected_ip(self, task_id: UUID, lease_token: UUID,
                                  node_id: str, address: str) -> bool:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            return False
        if not parsed.is_global or parsed.is_multicast or parsed.is_unspecified:
            return False
        return await self._repository.record_proxy_ip(
            task_id, lease_token, node_id, str(parsed),
        )

    async def mark_failure(self, node_id: str, outcome: str, *, task_id: UUID,
                           lease_token: UUID, cooldown_seconds: int = 300,
                           offline_after: int = 3) -> bool:
        return await self._repository.fail_proxy(
            task_id, lease_token, node_id, outcome, cooldown_seconds, offline_after,
        )

    async def release_proxy(self, task_id: UUID, lease_token: UUID) -> None:
        await self._repository.release_proxy(task_id, lease_token)
