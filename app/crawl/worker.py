import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app.crawl.classify import (
    FailureDecision,
    backoff_seconds,
    classify_failure,
    is_transient_exception,
)
from app.crawl.config import CrawlConfig
from app.crawl.discovery import discover_links
from app.crawl.types import TaskResult
from app.crawl.policy import classify_robots_response, robots_decision, robots_outcome
from app import fetch
from app.acquisition.router import HumanInputRequired
from app.acquisition.providers import ProviderFailure

logger = logging.getLogger(__name__)

_SAFE_ERROR_CHARS = 500


class CrawlWorker:
    """Execute one fenced crawl task at a time."""

    def __init__(self, worker_id: str, capabilities: set[str], repository: Any,
                 scraper: Any, *, heartbeat_seconds: float = 30,
                 discover: Callable = discover_links, robots_fetch: Callable = fetch.fetch_http,
                 robots_sleep: Callable = asyncio.sleep, proxy_pool: Any = None,
                 acquisition_router: Any = None, session_handler: Any = None):
        self.worker_id = worker_id
        self.capabilities = capabilities
        self.repository = repository
        self.scraper = scraper
        self.heartbeat_seconds = heartbeat_seconds
        self.discover = discover
        self.robots_fetch = robots_fetch
        self.robots_sleep = robots_sleep
        self.proxy_pool = proxy_pool
        self.acquisition_router = acquisition_router
        self.session_handler = session_handler
        self.active_lease: tuple[Any, Any] | None = None
        self.active_proxy_lease: Any | None = None

    async def run_once(self) -> bool:
        task = await self.repository.claim_task(self.worker_id, self.capabilities)
        if task is None:
            return False
        self.active_lease = (task.id, task.lease_token)

        config = CrawlConfig.model_validate(task.config)
        lease_lost = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(task, lease_lost))
        scrape_task = None
        lease_wait = None
        proxy_lease = None
        try:
            async def reserve_browser() -> bool:
                return await self.repository.reserve_browser_navigation(
                    task.id, task.lease_token
                )

            timeout = (task.deadline_at - datetime.now(timezone.utc)).total_seconds()
            if timeout <= 0:
                await self.repository.heartbeat(task.id, task.lease_token)
                return True
            if config.respectRobots and not await self._robots_allowed(task, config):
                return True
            if lease_lost.is_set() or task.deadline_at <= datetime.now(timezone.utc):
                if not lease_lost.is_set():
                    await self.repository.heartbeat(task.id, task.lease_token)
                return True
            if self.proxy_pool is not None and "proxy" in task.required_capabilities:
                proxy_lease = await self.proxy_pool.select(
                    task.origin_key, task_id=task.id, lease_token=task.lease_token,
                )
                if proxy_lease is None:
                    await self._record_failure(
                        task, {"metadata": {"reason": "transport_error"}}, None,
                    )
                    return True
                async def attest(address: str) -> bool:
                    return await self.proxy_pool.record_connected_ip(
                        task.id, task.lease_token, proxy_lease.node_id, address,
                    )
                await proxy_lease.start(attest)
                self.active_proxy_lease = proxy_lease
            timeout = (task.deadline_at - datetime.now(timezone.utc)).total_seconds()
            # Production always acquires through the router (TaskResult). Direct
            # scraper.scrape is not a second path — tests inject a fake router.
            if self.acquisition_router is None:
                raise RuntimeError(
                    "CrawlWorker requires an acquisition_router; "
                    "direct scraper.scrape is not a durable acquire path"
                )
            acquire_options: dict[str, Any] = {
                "only_main_content": config.onlyMainContent,
                "engine": config.engine,
                "capture_screenshot": config.screenshots,
                "max_decoded_bytes": task.byte_allowance,
                "before_browser": reserve_browser,
            }
            if proxy_lease is not None:
                # Proxy workers never fall back to ambient or direct proxy settings.
                acquire_options["proxy"] = proxy_lease.playwright_proxy()
                acquire_options["trust_env"] = False
            scrape_task = asyncio.create_task(
                self.acquisition_router.acquire(task, options=acquire_options)
            )
            lease_wait = asyncio.create_task(lease_lost.wait())
            done, _ = await asyncio.wait(
                {scrape_task, lease_wait}, timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if scrape_task not in done:
                if not lease_lost.is_set():
                    await self.repository.heartbeat(task.id, task.lease_token)
                scrape_task.cancel()
                await asyncio.gather(scrape_task, return_exceptions=True)
                return True
            lease_wait.cancel()
            await asyncio.gather(lease_wait, return_exceptions=True)
            try:
                acquired: TaskResult = await scrape_task
            except HumanInputRequired as exc:
                if (exc.backend != "owned" or "browser" not in self.capabilities
                        or self.session_handler is None):
                    raise
                if not await self.session_handler.start(task, acquire_options):
                    raise
                return True
            except ProviderFailure as exc:
                decision = FailureDecision(
                    exc.retryable,
                    "transport" if exc.retryable else "permanent",
                    exc.code,
                )
                await self._record_failure(
                    task,
                    {"metadata": {
                        "reason": exc.code,
                        "status_code": exc.status_code,
                        "retry_after": exc.retry_after_seconds,
                    }},
                    proxy_lease,
                    decision=decision,
                )
                return True
            if lease_lost.is_set():
                return True

            metadata = dict(acquired.metadata or {})
            if proxy_lease is not None:
                metadata["proxy_id"] = proxy_lease.node_id
            page_url = acquired.final_url or task.url
            status_code = acquired.status_code
            if status_code is None:
                status_code = metadata.get("status_code")
            links = self.discover(acquired.discovery_html or "", page_url, config)
            result = TaskResult(
                final_url=page_url,
                status_code=status_code if isinstance(status_code, int) else None,
                title=acquired.title or "",
                markdown=acquired.markdown or "",
                metadata=metadata,
                discovered_urls=tuple(link.url for link in links),
                discovery_html=acquired.discovery_html or "",
            )
            if not lease_lost.is_set():
                await self.repository.complete_task(task.id, task.lease_token, result)
            return True
        except Exception as exc:
            # Infrastructure I/O (e.g. DB blip on complete_task) is retryable;
            # programming defects stay permanent worker_exception.
            if is_transient_exception(exc):
                reason = "transport_error"
            else:
                reason = "worker_exception"
            logger.exception(
                "crawl task raised unexpected exception worker_id=%s task_id=%s "
                "job_id=%s url=%s reason=%s",
                self.worker_id,
                getattr(task, "id", None),
                getattr(task, "job_id", None),
                getattr(task, "url", None),
                reason,
            )
            if not lease_lost.is_set():
                await self._record_failure(
                    task,
                    {
                        "metadata": {
                            "reason": reason,
                            "exception_type": type(exc).__name__,
                            "error": str(exc)[:_SAFE_ERROR_CHARS],
                        }
                    },
                    proxy_lease,
                )
            return True
        finally:
            if lease_wait is not None and not lease_wait.done():
                lease_wait.cancel()
                await asyncio.gather(lease_wait, return_exceptions=True)
            if scrape_task is not None and not scrape_task.done():
                scrape_task.cancel()
                await asyncio.gather(scrape_task, return_exceptions=True)
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
            self.active_lease = None
            self.active_proxy_lease = None
            if proxy_lease is not None:
                await proxy_lease.close()
            await self._release_proxy(task, proxy_lease)

    async def _heartbeat(self, task: Any, lease_lost: asyncio.Event) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            if not await self.repository.heartbeat(task.id, task.lease_token):
                lease_lost.set()
                return

    async def _record_failure(
        self, task: Any, scraped: dict, proxy_lease: Any = None,
        *, decision: FailureDecision | None = None,
    ) -> None:
        metadata = scraped.get("metadata") or {}
        decision = decision or classify_failure(
            metadata.get("reason", "transport_error"), metadata.get("status_code")
        )
        if proxy_lease is not None:
            if metadata.get("reason") in {"blocked", "blocked_challenge"}:
                await self.proxy_pool.mark_failure(
                    proxy_lease.node_id, "blocked", task_id=task.id,
                    lease_token=task.lease_token,
                )
            elif decision.error_class == "transport":
                await self.proxy_pool.mark_failure(
                    proxy_lease.node_id, "transport", task_id=task.id,
                    lease_token=task.lease_token,
                )
        if decision.retry:
            delay = backoff_seconds(task.attempt, metadata.get("retry_after"))
            await self.repository.retry_task(
                task.id, task.lease_token, decision,
                datetime.now(timezone.utc) + timedelta(seconds=delay),
                metadata,
            )
        else:
            await self.repository.fail_task(task.id, task.lease_token, decision, metadata)

    async def _release_proxy(self, task: Any, proxy_lease: Any) -> None:
        if proxy_lease is not None:
            await self.proxy_pool.release_proxy(task.id, task.lease_token)

    async def _robots_allowed(self, task: Any, config: CrawlConfig) -> bool:
        if not hasattr(self.repository, "robots_cache"):
            return True
        cache = await self.repository.robots_cache(task.origin_key)
        now = datetime.now(timezone.utc)
        body = cache.get("robots_body") if cache else None
        expires = cache.get("robots_expires_at") if cache else None
        fetched = body is None or expires is None or expires <= now
        if fetched:
            response = await self.robots_fetch(task.origin_key + "/robots.txt")
            status = response.get("status") if response else None
            outcome = classify_robots_response(
                status, now=now,
                retry_after=(response or {}).get("headers", {}).get("retry-after"),
            )
            if outcome.action == "parse":
                body = response.get("html", "")
                await self.repository.store_robots(task.origin_key, body, status)
            elif outcome.action == "allow":
                body = ""
                await self.repository.store_robots(task.origin_key, body, status)
            elif outcome.action == "deny":
                body = "User-agent: *\nDisallow: /\n"
                await self.repository.store_robots(task.origin_key, body, status)
            elif config.robotsFailOpen:
                body = ""
            else:
                await self.repository.retry_task(
                    task.id, task.lease_token, classify_failure("transport_error", status),
                    outcome.retry_at or now + timedelta(seconds=60),
                )
                return False
        if fetched and config.minDelayMs:
            await self.robots_sleep(config.minDelayMs / 1000)
        decision = robots_outcome(
            allowed=robots_decision(body, "CrawlTrove", task.url), is_seed=task.depth == 0,
        )
        if decision.allowed:
            return True
        await self.repository.block_robots(task.id, task.lease_token, decision.code)
        return False
