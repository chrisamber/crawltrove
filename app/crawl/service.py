import asyncio
import os
from typing import Optional

from app.crawl import repository
from app.crawl.config import CrawlConfig
from app.crawl.worker import CrawlWorker
from app.services import scraper
from app.url_safety import ensure_public_url


class CrawlService:
    """Single-host durable crawl worker and maintenance loops."""

    def __init__(self, worker: Optional[CrawlWorker] = None):
        self._wake = asyncio.Event()
        self._worker = worker or CrawlWorker(
            os.environ.get("CRAWL_WORKER_ID", "crawl-worker-1"),
            {"http", "browser"}, repository, scraper,
        )
        self._worker_task = None
        self._maintenance_task = None

    async def submit_crawl(self, config: CrawlConfig, *,
                           idempotency_key: str | None = None,
                           definition_job_id: int | None = None,
                           trigger: str = "manual"):
        await repository.require_pool()
        await ensure_public_url(config.url)
        job_id = await repository.submit_job(
            config, idempotency_key=idempotency_key,
            definition_job_id=definition_job_id, trigger=trigger,
        )
        self._wake.set()
        return job_id

    async def start(self) -> None:
        if self._maintenance_task is not None:
            return
        remote_workers = os.environ.get("CRAWLTROVE_REMOTE_WORKERS", "").lower() in {
            "1", "true", "yes", "on",
        }
        if not remote_workers:
            if os.environ.get("PROXY_POOLS_FILE", "").strip():
                from app.acquisition.proxy import ProxyPool
                self._worker.proxy_pool = ProxyPool.from_environment(
                    await repository.require_pool(),
                )
                self._worker.capabilities.add("proxy")
            self._worker_task = asyncio.create_task(self._run_worker())
        self._maintenance_task = asyncio.create_task(self._run_maintenance())

    async def stop(self) -> None:
        self._wake.set()
        tasks = [task for task in (self._worker_task, self._maintenance_task) if task]
        self._worker_task = self._maintenance_task = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_worker(self) -> None:
        while True:
            self._wake.clear()
            try:
                if await self._worker.run_once():
                    continue
            except Exception:
                pass
            try:
                async with asyncio.timeout(1):
                    await self._wake.wait()
            except TimeoutError:
                pass

    async def _run_maintenance(self) -> None:
        while True:
            await asyncio.sleep(10)
            try:
                await repository.reap_expired_leases()
                await repository.finalize_jobs()
            except Exception:
                pass


crawl_service = CrawlService()


async def submit_crawl(config: CrawlConfig, **kwargs):
    return await crawl_service.submit_crawl(config, **kwargs)
