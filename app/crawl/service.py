import asyncio
import math
import os
from typing import Optional

from app.crawl import repository
from app.crawl.config import CrawlConfig
from app.crawl.worker import CrawlWorker
from app.services import scraper
from app.url_safety import ensure_public_url
from app.acquisition.registry import env_registry
from app.acquisition.router import AcquisitionRouter


class ProviderBudgetInvalid(ValueError):
    """The selected provider cannot run within the submitted native budget."""


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
        self.registry = env_registry(scraper)
        self._worker.acquisition_router = AcquisitionRouter(self.registry, repository, scraper)
        self._add_available_route_capabilities()

    def _add_available_route_capabilities(self) -> None:
        capabilities = getattr(self._worker, "capabilities", None)
        if capabilities is None:
            return
        capabilities.update(self.registry.available_routes("auto"))

    async def submit_crawl(self, config: CrawlConfig, *,
                           idempotency_key: str | None = None,
                           definition_job_id: int | None = None,
                           trigger: str = "manual"):
        await repository.require_pool()
        provider = config.acquisition.provider
        if provider != "auto" and provider != "local":
            self.registry.require_available(provider)
            budgets = config.acquisition.creditBudgets
            if provider == "firecrawl" and (
                budgets.firecrawl is None or budgets.firecrawl.credits < 1
            ):
                raise ProviderBudgetInvalid("firecrawl credit budget must be at least 1")
            if provider == "brightdata" and (
                budgets.brightdata is None or budgets.brightdata.requests < 1
            ):
                raise ProviderBudgetInvalid("brightdata request budget must be at least 1")
            if provider == "browserbase":
                required = math.ceil(config.timeoutSeconds / 60)
                if (config.timeoutSeconds < 60 or budgets.browserbase is None
                        or budgets.browserbase.proxyBytes != 0
                        or budgets.browserbase.browserMinutes < required):
                    raise ProviderBudgetInvalid(
                        "browserbase requires timeoutSeconds>=60, proxyBytes=0, "
                        "and sufficient browserMinutes"
                    )
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
                self.registry.set_proxy_pool(self._worker.proxy_pool)
                self._worker.capabilities.add("proxy")
                self._add_available_route_capabilities()
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
        await self.registry.aclose()

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
