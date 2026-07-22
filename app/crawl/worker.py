import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app.crawl.classify import backoff_seconds, classify_failure
from app.crawl.config import CrawlConfig
from app.crawl.discovery import discover_links
from app.crawl.types import TaskResult
from app.crawl.policy import classify_robots_response, robots_decision, robots_outcome
from app import fetch


class CrawlWorker:
    """Execute one fenced crawl task at a time."""

    def __init__(self, worker_id: str, capabilities: set[str], repository: Any,
                 scraper: Any, *, heartbeat_seconds: float = 30,
                 discover: Callable = discover_links, robots_fetch: Callable = fetch.fetch_http,
                 robots_sleep: Callable = asyncio.sleep):
        self.worker_id = worker_id
        self.capabilities = capabilities
        self.repository = repository
        self.scraper = scraper
        self.heartbeat_seconds = heartbeat_seconds
        self.discover = discover
        self.robots_fetch = robots_fetch
        self.robots_sleep = robots_sleep

    async def run_once(self) -> bool:
        task = await self.repository.claim_task(self.worker_id, self.capabilities)
        if task is None:
            return False

        config = CrawlConfig.model_validate(task.config)
        lease_lost = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(task, lease_lost))
        scrape_task = None
        lease_wait = None
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
            timeout = (task.deadline_at - datetime.now(timezone.utc)).total_seconds()
            scrape_task = asyncio.create_task(self.scraper.scrape(
                task.url,
                only_main_content=config.onlyMainContent,
                engine=config.engine,
                capture_screenshot=config.screenshots,
                max_decoded_bytes=task.byte_allowance,
                before_browser=reserve_browser,
            ))
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
            scraped = await scrape_task
            if lease_lost.is_set():
                return True
            if not scraped.get("success"):
                await self._record_failure(task, scraped)
                return True

            metadata = dict(scraped.get("metadata") or {})
            raw = scraped.get("_raw") or {}
            if raw.get("screenshot"):
                metadata["screenshot_bytes"] = len(raw["screenshot"])
            page_url = scraped.get("url") or task.url
            links = self.discover(scraped.get("discovery_html", ""), page_url, config)
            result = TaskResult(
                final_url=page_url,
                status_code=metadata.get("status_code"),
                title=scraped.get("title", ""),
                markdown=scraped.get("markdown", ""),
                metadata=metadata,
                discovered_urls=tuple(link.url for link in links),
            )
            if not lease_lost.is_set():
                await self.repository.complete_task(task.id, task.lease_token, result)
            return True
        except Exception as exc:
            if not lease_lost.is_set():
                await self._record_failure(task, {"error": str(exc)})
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

    async def _heartbeat(self, task: Any, lease_lost: asyncio.Event) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            if not await self.repository.heartbeat(task.id, task.lease_token):
                lease_lost.set()
                return

    async def _record_failure(self, task: Any, scraped: dict) -> None:
        metadata = scraped.get("metadata") or {}
        decision = classify_failure(
            metadata.get("reason", "transport_error"), metadata.get("status_code")
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
