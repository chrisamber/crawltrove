import asyncio
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.artifacts.base import ArtifactRef, ArtifactIntegrityError
from app.crawl.types import ClaimedTask, TaskResult
from app.crawl.remote_repository import RemoteRepository
from app.crawl.worker import CrawlWorker
from app.worker_config import WorkerConfig
from app.worker_main import WorkerRuntime, _ArtifactRepository


class FakeRepository:
    def __init__(self, state="active"):
        self.state = state
        self.register_calls = 0
        self.drain_calls = 0
        self.release_calls = []
        self.heartbeat_ok = True

    async def register(self, protocol_version, capabilities):
        self.register_calls += 1
        return {"state": self.state}

    async def drain(self):
        self.drain_calls += 1

    async def heartbeat(self, task_id, lease_token):
        return self.heartbeat_ok

    async def release(self, task_id, lease_token):
        self.release_calls.append((task_id, lease_token))


class FakeWorker:
    def __init__(self, repository):
        self.repository = repository
        self.claim_calls = 0

    async def run_once(self):
        self.claim_calls += 1
        return True


class HeartbeatLossWorker(FakeWorker):
    async def run_once(self):
        self.claim_calls += 1
        await self.repository.heartbeat("task", "token")
        if self.repository.heartbeat_ok:
            self.repository.complete_calls += 1
        return True


class FakeArtifacts:
    ready = True


class RecordingArtifacts(FakeArtifacts):
    def __init__(self, verified=True):
        self.verified = verified
        self.put_calls = []

    async def put(self, chunks, media_type, expected_max_bytes):
        self.put_calls.append((b"".join([chunk async for chunk in chunks]), media_type, expected_max_bytes))
        return ArtifactRef(
            uri="s3://crawl/workers/edge-1/sha256/aa/" + "a" * 64,
            size=5,
            sha256="a" * 64,
            media_type=media_type,
        )

    async def verify(self, ref):
        return self.verified


class CompletingRepository:
    def __init__(self):
        self.complete_calls = []

    async def claim_task(self, worker_id, capabilities):
        from types import SimpleNamespace
        return SimpleNamespace(id="task", artifact_allowance=12)

    async def complete_task(self, task_id, lease_token, result, artifact_ref=None):
        self.complete_calls.append((task_id, lease_token, result, artifact_ref))
        return True


def worker_config():
    return WorkerConfig.from_mapping({
        "workerId": "edge-1", "databaseUrl": "postgresql://edge@db/crawl",
        "capabilities": ["http"], "protocolVersion": 1,
        "artifactPrefix": "workers/edge-1/",
    })


@pytest.fixture
def insecure_config(monkeypatch):
    monkeypatch.setenv("WORKER_ALLOW_INSECURE_DB", "true")
    return worker_config()


async def test_incompatible_worker_never_claims(insecure_config):
    repository = FakeRepository(state="incompatible")
    worker = FakeWorker(repository)
    runtime = WorkerRuntime(insecure_config, repository, FakeArtifacts(), worker=worker)
    await runtime.tick()
    assert worker.claim_calls == 0
    assert runtime.readiness()["protocol"] == "incompatible"
    assert runtime.readiness()["database"] == "up"


async def test_lost_heartbeat_discards_result(insecure_config):
    repository = FakeRepository()
    repository.complete_calls = 0
    repository.heartbeat_ok = False
    worker = HeartbeatLossWorker(repository)
    runtime = WorkerRuntime(insecure_config, repository, FakeArtifacts(), worker=worker)
    await runtime.tick()
    assert repository.complete_calls == 0
    assert runtime.readiness()["database"] == "down"


async def test_remote_worker_enables_only_enrolled_managed_routes(insecure_config, monkeypatch):
    from app.acquisition.router import AcquisitionRouter

    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-key")
    config = WorkerConfig.from_mapping({
        "workerId": "edge-1", "databaseUrl": "postgresql://edge@db/crawl",
        "capabilities": ["http", "firecrawl_scrape"], "protocolVersion": 1,
        "artifactPrefix": "workers/edge-1/",
    })
    runtime = WorkerRuntime(config, FakeRepository(), FakeArtifacts())
    try:
        assert isinstance(runtime.worker.acquisition_router, AcquisitionRouter)
        assert runtime.registry.route_available("local_http") is True
        assert runtime.registry.route_available("firecrawl_scrape") is True
        assert runtime.registry.route_available("firecrawl_interact") is False
        assert runtime.registry.route_available("brightdata_unlocker") is False
        assert "firecrawl_scrape" in runtime.worker.capabilities
    finally:
        await runtime.close()


async def test_health_server_reports_ready_and_live(insecure_config):
    repository = FakeRepository()
    runtime = WorkerRuntime(insecure_config, repository, FakeArtifacts(), worker=FakeWorker(repository))
    await runtime.tick()
    await runtime.start_health_server(port=0)
    port = runtime.health_port
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /health/ready HTTP/1.1\r\nHost: test\r\n\r\n")
        await writer.drain()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()
        headers, body = response.split(b"\r\n\r\n", 1)
        assert b"200 OK" in headers
        assert json.loads(body)["ready"] is True
        assert json.loads(body)["security"] == "degraded"
    finally:
        await runtime.close()


def test_remote_repository_exposes_worker_policy_boundaries():
    for method in (
        "reserve_browser_navigation", "robots_cache", "store_robots", "block_robots",
        "assign_proxy", "record_proxy_ip", "fail_proxy", "release_proxy",
    ):
        assert callable(getattr(RemoteRepository, method))


async def test_drain_stops_future_claims(insecure_config):
    repository = FakeRepository()
    worker = FakeWorker(repository)
    runtime = WorkerRuntime(insecure_config, repository, FakeArtifacts(), worker=worker)
    await runtime.drain()
    await runtime.tick()
    assert repository.drain_calls == 1
    assert worker.claim_calls == 0


async def test_drain_releases_unfinished_active_lease(insecure_config):
    started = asyncio.Event()

    class BlockingWorker(FakeWorker):
        active_lease = None

        async def run_once(self):
            self.active_lease = ("task", "token")
            started.set()
            await asyncio.Event().wait()

    repository = FakeRepository()
    worker = BlockingWorker(repository)
    runtime = WorkerRuntime(
        insecure_config, repository, FakeArtifacts(), worker=worker,
        drain_timeout_seconds=0.01,
    )
    run = asyncio.create_task(runtime.run())
    await started.wait()
    await runtime.drain()
    await run
    assert repository.release_calls == [("task", "token")]


async def test_artifact_adapter_uploads_verifies_then_completes():
    repository = CompletingRepository()
    artifacts = RecordingArtifacts()
    adapter = _ArtifactRepository(repository, artifacts)
    await adapter.claim_task("edge-1", {"http"})
    result = TaskResult("https://example.com", 200, "title", "hello")
    assert await adapter.complete_task("task", "token", result) is True
    assert artifacts.put_calls == [(b"hello", "text/markdown", 12)]
    assert repository.complete_calls[0][3] == {
        "uri": "s3://crawl/workers/edge-1/sha256/aa/" + "a" * 64,
        "size": 5,
        "sha256": "a" * 64,
        "media_type": "text/markdown",
    }


async def test_artifact_adapter_refuses_unverified_upload_before_completion():
    repository = CompletingRepository()
    adapter = _ArtifactRepository(repository, RecordingArtifacts(verified=False))
    await adapter.claim_task("edge-1", {"http"})
    with pytest.raises(ArtifactIntegrityError):
        await adapter.complete_task("task", "token", TaskResult("url", 200, "", "hello"))
    assert repository.complete_calls == []


async def test_artifact_completion_failure_is_retried_by_crawl_worker():
    class Repository(CompletingRepository):
        def __init__(self):
            super().__init__()
            self.retries = []
            self.task = ClaimedTask(
                id=uuid4(), job_id=uuid4(), url="https://example.com",
                normalized_url="https://example.com", origin_key="https://example.com:443",
                depth=0, attempt=1, lease_token=uuid4(),
                deadline_at=datetime.now(timezone.utc) + timedelta(minutes=1),
                config={"url": "https://example.com", "respectRobots": False},
                byte_allowance=1024, artifact_allowance=12,
            )

        async def claim_task(self, worker_id, capabilities):
            task, self.task = self.task, None
            return task

        async def heartbeat(self, task_id, lease_token):
            return True

        async def complete_task(self, task_id, lease_token, result, artifact_ref=None):
            raise OSError("database temporarily unavailable")

        async def retry_task(self, task_id, lease_token, decision, available_at, metadata=None):
            self.retries.append(decision)
            return True

    class Scraper:
        async def scrape(self, url, **kwargs):
            return {"success": True, "url": url, "markdown": "hello", "metadata": {}}

    repository = Repository()
    adapter = _ArtifactRepository(repository, RecordingArtifacts())
    worker = CrawlWorker("edge-1", {"http"}, adapter, Scraper())
    assert await worker.run_once() is True
    assert repository.retries[0].retry is True
