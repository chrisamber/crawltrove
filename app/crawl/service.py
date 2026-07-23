import asyncio
import logging
import math
import os
import time
from typing import Optional

from app.crawl import repository
from app.crawl.config import CrawlConfig
from app.crawl.worker import CrawlWorker
from app.services import scraper
from app.url_safety import ensure_public_url
from app.acquisition.registry import env_registry
from app.acquisition.router import AcquisitionRouter
from app.acquisition import sessions

logger = logging.getLogger(__name__)


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
        self._maintenance_last_success: float | None = None
        self._maintenance_last_error: str | None = None
        self._session_runner = None
        self._started = False
        self._start_error: str | None = None
        self.registry = env_registry(scraper)
        self._worker.acquisition_router = AcquisitionRouter(self.registry, repository, scraper)
        self._add_available_route_capabilities()

    def _add_available_route_capabilities(self) -> None:
        capabilities = getattr(self._worker, "capabilities", None)
        if capabilities is None:
            return
        capabilities.update(self.registry.available_routes("auto"))

    def wake(self) -> None:
        """Nudge the worker loop after enqueue / retry."""
        self._wake.set()

    def maintenance_status(self) -> dict:
        """Public snapshot for readiness and operations endpoints."""
        task = self._maintenance_task
        return {
            "started": self._started,
            "start_error": self._start_error,
            "running": task is not None and not task.done(),
            "last_success": self._maintenance_last_success,
            "last_error": self._maintenance_last_error,
        }

    def worker_browser(self):
        """Playwright browser handle when the in-process worker owns one."""
        return getattr(getattr(self._worker, "scraper", None), "browser", None)

    def leases_ready(self, *, max_age_seconds: float = 30) -> bool:
        status = self.maintenance_status()
        if status.get("start_error") or not status.get("started"):
            return False
        last_success = status.get("last_success")
        return (
            bool(status.get("running"))
            and last_success is not None
            and time.monotonic() - float(last_success) <= max_age_seconds
        )

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
        self.wake()
        return job_id

    async def start(self) -> None:
        if self._maintenance_task is not None:
            self._started = True
            self._start_error = None
            return
        try:
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
                if ("browser" in getattr(self._worker, "capabilities", set())
                        and getattr(self._worker, "scraper", None) is not None):
                    from app.acquisition.owned_session import OwnedSessionRunner
                    from app.artifacts import artifact_store
                    self._session_runner = OwnedSessionRunner(
                        self._worker.worker_id, repository, self._worker.scraper, artifact_store(),
                    )
                    self._worker.session_handler = self._session_runner
                self._worker_task = asyncio.create_task(self._run_worker())
            self._maintenance_task = asyncio.create_task(self._run_maintenance())
            self._started = True
            self._start_error = None
        except Exception as exc:
            message = str(exc).strip()
            detail = type(exc).__name__
            if message:
                detail = f"{detail}: {message[:300]}"
            self._started = False
            self._start_error = detail
            logger.exception("durable crawl service failed to start detail=%s", detail)
            raise

    async def stop(self) -> None:
        self.wake()
        tasks = [task for task in (self._worker_task, self._maintenance_task) if task]
        self._worker_task = self._maintenance_task = None
        self._started = False
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._session_runner is not None:
            await self._session_runner.close()
            self._session_runner = None
            self._worker.session_handler = None
        await self.registry.aclose()

    async def _run_worker(self) -> None:
        worker_id = getattr(self._worker, "worker_id", None)
        while True:
            self._wake.clear()
            try:
                if await self._worker.run_once():
                    continue
            except Exception:
                # Keep the loop alive, but never discard diagnostics.
                active = getattr(self._worker, "active_lease", None)
                task_id = active[0] if isinstance(active, tuple) and active else None
                logger.exception(
                    "crawl worker loop failed worker_id=%s task_id=%s",
                    worker_id,
                    task_id,
                )
            try:
                async with asyncio.timeout(1):
                    await self._wake.wait()
            except TimeoutError:
                pass

    async def _run_maintenance(self) -> None:
        while True:
            try:
                await repository.reap_expired_leases()
                await sessions.expire_due()
                await repository.finalize_jobs()
                self._maintenance_last_success = time.monotonic()
                self._maintenance_last_error = None
            except Exception as exc:
                message = str(exc).strip()
                detail = type(exc).__name__
                if message:
                    detail = f"{detail}: {message[:300]}"
                self._maintenance_last_error = detail
                logger.exception("crawl maintenance loop failed detail=%s", detail)
            await asyncio.sleep(10)


crawl_service = CrawlService()


async def submit_crawl(config: CrawlConfig, **kwargs):
    return await crawl_service.submit_crawl(config, **kwargs)
