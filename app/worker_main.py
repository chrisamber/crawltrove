"""Process entry point and small health surface for remote crawl workers."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
from dataclasses import asdict
import json
import os
import signal
import time
from typing import Any

from app.crawl.worker import CrawlWorker
from app.acquisition.proxy import RemoteProxyPool
from app.acquisition.registry import env_registry
from app.acquisition.router import AcquisitionRouter
from app.artifacts.base import ArtifactIntegrityError
from app.scraper import WebScraper
from app.worker_config import WorkerConfig


class _HeartbeatRepository:
    """Observe lease loss without widening the remote repository surface."""

    def __init__(self, repository: Any, runtime: "WorkerRuntime") -> None:
        self._repository = repository
        self._runtime = runtime

    def __getattr__(self, name: str) -> Any:
        return getattr(self._repository, name)

    async def heartbeat(self, task_id: Any, lease_token: Any) -> bool:
        ok = await self._repository.heartbeat(task_id, lease_token)
        if not ok:
            self._runtime._database = "down"
        return ok


class _ArtifactRepository:
    """Attach verified immutable Markdown artifacts to remote completions."""

    def __init__(self, repository: Any, artifacts: Any) -> None:
        self._repository = repository
        self._artifacts = artifacts
        self._allowances: dict[Any, int] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._repository, name)

    async def claim_task(self, worker_id: str, capabilities: set[str]) -> Any:
        task = await self._repository.claim_task(worker_id, capabilities)
        if task is not None:
            self._allowances[task.id] = task.artifact_allowance
        return task

    async def complete_task(self, task_id: Any, lease_token: Any, result: Any) -> bool:
        allowance = self._allowances.get(task_id)
        if allowance is None:
            raise ValueError("remote completion has no claimed artifact allowance")

        async def markdown():
            yield result.markdown.encode("utf-8")

        artifact_ref = await self._artifacts.put(markdown(), "text/markdown", allowance)
        if not await self._artifacts.verify(artifact_ref):
            raise ArtifactIntegrityError("uploaded artifact did not verify")
        completed = await self._repository.complete_task(
            task_id, lease_token, result, artifact_ref=asdict(artifact_ref),
        )
        if completed:
            self._allowances.pop(task_id, None)
        return completed

    async def retry_task(self, task_id: Any, lease_token: Any, decision: Any,
                         available_at: Any, metadata: Any = None) -> bool:
        self._allowances.pop(task_id, None)
        return await self._repository.retry_task(
            task_id, lease_token, decision, available_at, metadata,
        )

    async def fail_task(self, task_id: Any, lease_token: Any, decision: Any,
                        metadata: Any = None) -> bool:
        self._allowances.pop(task_id, None)
        return await self._repository.fail_task(task_id, lease_token, decision, metadata)

    async def release(self, task_id: Any, lease_token: Any) -> bool:
        self._allowances.pop(task_id, None)
        return await self._repository.release(task_id, lease_token)


class WorkerRuntime:
    """Register, claim work only when compatible, and expose local health."""

    def __init__(self, config: WorkerConfig, repository: Any, artifacts: Any,
                 *, scraper: Any = None, worker: Any = None,
                 poll_seconds: float = 1.0, drain_timeout_seconds: float = 30.0) -> None:
        self.config = config
        self.repository = repository
        self.artifacts = artifacts
        self.scraper = scraper or WebScraper()
        self.poll_seconds = poll_seconds
        self.drain_timeout_seconds = drain_timeout_seconds
        self.draining = False
        self._database = "unknown"
        self._protocol = "unknown"
        self._artifacts = "unknown"
        self._browser = "not_required"
        self._next_registration_at = 0.0
        self._health_server: asyncio.AbstractServer | None = None
        self.health_port: int | None = None
        self._stop = asyncio.Event()
        artifact_repository = _ArtifactRepository(repository, artifacts)
        monitored_repository = _HeartbeatRepository(artifact_repository, self)
        proxy_pool = (RemoteProxyPool.from_environment(monitored_repository)
                      if "proxy" in config.capabilities else None)
        allowed_routes = {"local_http"}
        if "browser" in config.capabilities:
            allowed_routes.add("local_browser")
        if proxy_pool is not None:
            allowed_routes.add("owned_proxy_http")
        allowed_routes.update(set(config.capabilities) & {
            "firecrawl_scrape", "firecrawl_interact", "brightdata_unlocker",
            "browserbase_session",
        })
        self.registry = env_registry(
            self.scraper, proxy_pool=proxy_pool, allowed_routes=allowed_routes,
        )
        self.worker = worker or CrawlWorker(
            config.worker_id, set(config.capabilities), monitored_repository, self.scraper,
            proxy_pool=proxy_pool,
            acquisition_router=AcquisitionRouter(self.registry, monitored_repository, self.scraper),
        )

    async def tick(self) -> bool:
        """Perform at most one claim. Incompatible and draining workers never claim."""
        if self.draining:
            return False
        if not await self._register():
            return False
        await self._check_artifacts()
        if self._artifacts != "up":
            return False
        if not await self._check_browser():
            return False
        result = await self.worker.run_once()
        # Test doubles and repositories that can expose heartbeat state give the
        # health endpoint immediate lease-loss visibility. Production lease loss
        # is observed by _HeartbeatRepository above.
        if getattr(self.repository, "heartbeat_ok", True) is False:
            self._database = "down"
        return result

    async def _register(self) -> bool:
        now = time.monotonic()
        if self._protocol == "active" and self._database == "up":
            return True
        if now < self._next_registration_at:
            return False
        self._next_registration_at = now + 30
        try:
            registration = await self.repository.register(
                self.config.protocol_version,
                set(getattr(self.worker, "capabilities", self.config.capabilities)),
            )
        except Exception:
            self._database = "down"
            self._protocol = "unknown"
            return False
        state = registration.get("state") if isinstance(registration, dict) else registration
        self._protocol = str(state or "incompatible")
        self._database = "up"
        return self._protocol == "active"

    async def _check_artifacts(self) -> None:
        try:
            check = getattr(self.artifacts, "healthcheck", None)
            if check is not None:
                result = check()
                if asyncio.iscoroutine(result):
                    result = await result
                self._artifacts = "up" if result else "down"
            else:
                self._artifacts = "up" if getattr(self.artifacts, "ready", True) else "down"
        except Exception:
            self._artifacts = "down"

    async def _check_browser(self) -> bool:
        if "browser" not in self.config.capabilities:
            self._browser = "not_required"
            return True
        try:
            browser = getattr(self.scraper, "browser", None)
            if browser is None:
                self._browser = "down"
                return False
            await browser.start()
            self._browser = "up"
            return True
        except Exception:
            self._browser = "down"
            return False

    async def drain(self) -> None:
        """Stop taking claims and make the state visible to other processes."""
        if self.draining:
            return
        self.draining = True
        self._stop.set()
        try:
            await self.repository.drain()
        except Exception:
            self._database = "down"

    def readiness(self) -> dict[str, Any]:
        ready = (
            not self.draining
            and self._database == "up"
            and self._protocol == "active"
            and self._artifacts == "up"
            and self._browser in {"up", "not_required"}
        )
        return {
            "ready": ready,
            "database": self._database,
            "protocol": self._protocol,
            "artifacts": self._artifacts,
            "browser": self._browser,
            "security": self.config.security_state,
            "draining": self.draining,
        }

    async def start_health_server(self, host: str | None = None, port: int | None = None) -> None:
        if self._health_server is not None:
            return
        host = host or os.getenv("WORKER_HEALTH_HOST", "127.0.0.1")
        port = port if port is not None else int(os.getenv("WORKER_HEALTH_PORT", "8081"))
        self._health_server = await asyncio.start_server(self._handle_health, host, port)
        socket = self._health_server.sockets[0] if self._health_server.sockets else None
        self.health_port = socket.getsockname()[1] if socket else port

    async def _handle_health(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        try:
            request = await asyncio.wait_for(reader.readline(), timeout=2)
            target = request.decode("ascii", "replace").split(" ", 2)[1] if request else ""
            if target == "/health/live":
                status, body = "200 OK", {"live": True}
            elif target == "/health/ready":
                body = self.readiness()
                status = "200 OK" if body["ready"] else "503 Service Unavailable"
            else:
                status, body = "404 Not Found", {"detail": "not found"}
            encoded = json.dumps(body, separators=(",", ":")).encode()
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(encoded)}\r\nConnection: close\r\n\r\n".encode() + encoded
            )
            await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def run(self) -> None:
        await self.start_health_server()
        try:
            while not self._stop.is_set():
                tick = asyncio.create_task(self.tick())
                stop_wait = asyncio.create_task(self._stop.wait())
                done, _ = await asyncio.wait(
                    {tick, stop_wait}, return_when=asyncio.FIRST_COMPLETED,
                )
                if tick in done:
                    stop_wait.cancel()
                    await asyncio.gather(stop_wait, return_exceptions=True)
                    await tick
                    if not self._stop.is_set():
                        await asyncio.sleep(self.poll_seconds)
                    continue

                # SIGTERM has put the database identity into draining state.
                # Let the fenced task finish while its own heartbeat continues,
                # then explicitly release the observed lease for retry.
                try:
                    await asyncio.wait_for(asyncio.shield(tick), self.drain_timeout_seconds)
                except asyncio.TimeoutError:
                    lease = getattr(self.worker, "active_lease", None)
                    tick.cancel()
                    await asyncio.gather(tick, return_exceptions=True)
                    if lease is not None:
                        try:
                            await self.repository.release(*lease)
                        except Exception:
                            self._database = "down"
                finally:
                    stop_wait.cancel()
                    await asyncio.gather(stop_wait, return_exceptions=True)
        finally:
            await self.close()

    async def close(self) -> None:
        self._stop.set()
        if self._health_server is not None:
            self._health_server.close()
            await self._health_server.wait_closed()
            self._health_server = None
        close = getattr(self.scraper, "close", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result
        await self.registry.aclose()


async def _main(config_path: str) -> None:
    config = WorkerConfig.from_file(config_path)
    os.environ["CRAWLTROVE_ROLE"] = "worker"
    os.environ["CRAWLTROVE_WORKER_ID"] = config.worker_id
    if config.artifact_settings.get("s3AccessKey"):
        os.environ["S3_ACCESS_KEY_ID"] = config.artifact_settings["s3AccessKey"]
    if config.artifact_settings.get("s3SecretKey"):
        os.environ["S3_SECRET_ACCESS_KEY"] = config.artifact_settings["s3SecretKey"]
    from app.artifacts import artifact_store
    from app.crawl.remote_repository import RemoteRepository
    import asyncpg

    pool = await asyncpg.create_pool(
        dsn=config.database_url, ssl=config.ssl_context, min_size=1, max_size=2,
    )
    runtime = WorkerRuntime(
        config, RemoteRepository(pool, set(config.capabilities)), artifact_store(config.worker_id),
    )
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, lambda: asyncio.create_task(runtime.drain()))
    try:
        await runtime.run()
    finally:
        await pool.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    asyncio.run(_main(args.config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
