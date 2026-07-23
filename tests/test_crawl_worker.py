from app.crawl.classify import backoff_seconds, classify_failure
from types import SimpleNamespace
from uuid import uuid4

from datetime import datetime, timedelta, timezone

from app.crawl.types import ClaimedTask
from app.crawl.worker import CrawlWorker


def test_retry_only_transient_failures():
    assert classify_failure("transport_error", None).retry is True
    assert classify_failure("http_status_error", 503).retry is True
    assert classify_failure("http_status_error", 404).retry is False
    assert classify_failure("unsafe_url", None).retry is False
    assert classify_failure("blocked_robots", None).retry is False


def test_failure_classification_uses_stable_error_codes():
    from app.crawl.classify import is_transient_exception

    assert classify_failure("timeout", None).error_class == "transport"
    assert classify_failure("timeout", None).error_code == "timeout"
    assert classify_failure(None, 429).error_code == "http_429"
    assert classify_failure(None, None).error_code == "unknown_failure"
    worker = classify_failure("worker_exception", None)
    assert worker.retry is False
    assert worker.error_class == "internal"
    assert worker.error_code == "worker_exception"
    assert is_transient_exception(OSError("database temporarily unavailable"))
    assert not is_transient_exception(AttributeError("missing"))


def test_backoff_uses_full_jitter_with_retry_after_bounds(monkeypatch):
    calls = []

    def uniform(lower, upper):
        calls.append((lower, upper))
        return upper / 2

    monkeypatch.setattr("app.crawl.classify.random.uniform", uniform)

    assert backoff_seconds(1) == 0.5
    assert backoff_seconds(10) == 30.0
    assert calls == [(0.0, 1), (0.0, 60.0)]
    assert backoff_seconds(3, retry_after=4.5) == 4.5
    assert backoff_seconds(3, retry_after=-1) == 0.0
    assert calls == [(0.0, 1), (0.0, 60.0)]


def _task():
    return ClaimedTask(
        id=uuid4(), job_id=uuid4(), url="https://example.com",
        normalized_url="https://example.com", origin_key="https://example.com:443",
        depth=0, attempt=1, lease_token=uuid4(),
        deadline_at=datetime.now(timezone.utc) + timedelta(hours=1),
        config={"url": "https://example.com", "screenshots": True},
        byte_allowance=1024, artifact_allowance=1024,
    )


async def test_worker_completes_one_task_with_ordered_discovery():
    class Repository:
        def __init__(self):
            self.task = _task()
            self.completed = []

        async def claim_task(self, worker_id, capabilities):
            task, self.task = self.task, None
            return task

        async def heartbeat(self, task_id, lease_token):
            return True

        async def reserve_browser_navigation(self, task_id, lease_token):
            return True

        async def complete_task(self, task_id, lease_token, result):
            self.completed.append(result)
            return True

    class Scraper:
        async def scrape(self, url, **kwargs):
            assert kwargs["capture_screenshot"] is True
            assert kwargs["max_decoded_bytes"] == 1024
            return {
                "success": True, "url": url, "title": "Example",
                "markdown": "ok", "discovery_html": "<a href='/next'>next</a>",
                "metadata": {"status_code": 200},
            }

    repository = Repository()
    worker = CrawlWorker(
        "worker-1", {"http"}, repository, Scraper(),
        discover=lambda html, url, config: [SimpleNamespace(url=url + "next")],
    )

    assert await worker.run_once() is True
    assert repository.completed[0].discovered_urls == ("https://example.comnext",)
    assert await worker.run_once() is False


async def test_worker_discards_output_after_heartbeat_loss():
    import asyncio

    heartbeat_called = asyncio.Event()

    class Repository:
        async def claim_task(self, worker_id, capabilities):
            return _task()

        async def heartbeat(self, task_id, lease_token):
            heartbeat_called.set()
            return False

        async def complete_task(self, task_id, lease_token, result):
            raise AssertionError("lost leases must not complete")

    class Scraper:
        async def scrape(self, url, **kwargs):
            await heartbeat_called.wait()
            return {"success": True, "url": url, "metadata": {}}

    worker = CrawlWorker(
        "worker-1", {"http"}, Repository(), Scraper(),
        heartbeat_seconds=0,
    )

    assert await worker.run_once() is True


async def test_worker_cancels_scrape_at_task_deadline():
    import asyncio

    task = _task()
    task = ClaimedTask(**{
        **task.__dict__, "deadline_at": datetime.now(timezone.utc) + timedelta(milliseconds=10),
    })
    cancelled = asyncio.Event()

    class Repository:
        async def claim_task(self, worker_id, capabilities):
            return task

        async def heartbeat(self, task_id, lease_token):
            return False

    class Scraper:
        async def scrape(self, url, **kwargs):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    assert await CrawlWorker("worker-1", {"http"}, Repository(), Scraper()).run_once()
    assert cancelled.is_set()


async def test_worker_retries_transient_scrape_failure():
    class Repository:
        def __init__(self):
            self.retried = []

        async def claim_task(self, worker_id, capabilities):
            return _task()

        async def heartbeat(self, task_id, lease_token):
            return True

        async def retry_task(self, task_id, lease_token, decision, available_at, metadata=None):
            self.retried.append(decision)
            return True

    class Scraper:
        async def scrape(self, url, **kwargs):
            return {
                "success": False,
                "metadata": {"reason": "transport_error", "status_code": None},
            }

    repository = Repository()
    worker = CrawlWorker("worker-1", {"http"}, repository, Scraper())

    assert await worker.run_once() is True
    assert repository.retried[0].error_code == "transport_error"


async def test_worker_persists_unexpected_exception_metadata():
    """Unexpected exceptions must reach fail_task metadata, not be lost as transport_error."""
    class Repository:
        def __init__(self):
            self.failed = []

        async def claim_task(self, worker_id, capabilities):
            return _task()

        async def heartbeat(self, task_id, lease_token):
            return True

        async def fail_task(self, task_id, lease_token, decision, metadata=None):
            self.failed.append((decision, dict(metadata or {})))
            return True

        async def retry_task(self, *args, **kwargs):
            raise AssertionError("worker exceptions must not retry as transport")

    class Scraper:
        async def scrape(self, url, **kwargs):
            raise AttributeError("missing adapter attribute")

    repository = Repository()
    worker = CrawlWorker("worker-1", {"http"}, repository, Scraper())

    assert await worker.run_once() is True
    decision, metadata = repository.failed[0]
    assert decision.retry is False
    assert decision.error_class == "internal"
    assert decision.error_code == "worker_exception"
    assert metadata["reason"] == "worker_exception"
    assert metadata["exception_type"] == "AttributeError"
    assert "missing adapter attribute" in metadata["error"]


async def test_worker_blocks_disallowed_robots_before_scraping():
    class Repository:
        def __init__(self):
            self.blocked = []

        async def claim_task(self, worker_id, capabilities):
            return _task()

        async def heartbeat(self, task_id, lease_token):
            return True

        async def robots_cache(self, origin_key):
            return {"robots_body": "User-agent: *\nDisallow: /\n",
                    "robots_expires_at": datetime.now(timezone.utc) + timedelta(hours=1)}

        async def block_robots(self, task_id, lease_token, code):
            self.blocked.append(code)
            return True

    class Scraper:
        async def scrape(self, url, **kwargs):
            raise AssertionError("robots-denied URLs must not fetch")

    repository = Repository()
    assert await CrawlWorker("worker-1", {"http"}, repository, Scraper()).run_once()
    assert repository.blocked == ["seed_blocked_by_robots"]


async def test_worker_paces_page_after_live_robots_fetch():
    delays = []

    async def sleep(seconds):
        delays.append(seconds)

    class Repository:
        async def claim_task(self, worker_id, capabilities): return _task()
        async def heartbeat(self, task_id, lease_token): return True
        async def robots_cache(self, origin_key): return None
        async def store_robots(self, origin_key, body, status): pass
        async def reserve_browser_navigation(self, task_id, lease_token): return True
        async def complete_task(self, task_id, lease_token, result): return True

    class Scraper:
        async def scrape(self, url, **kwargs):
            return {"success": True, "url": url, "markdown": "ok", "metadata": {}}

    async def robots(url):
        return {"status": 200, "html": "User-agent: *\nAllow: /\n", "headers": {}}

    await CrawlWorker("worker-1", {"http"}, Repository(), Scraper(),
                      robots_fetch=robots, robots_sleep=sleep).run_once()
    assert delays == [1.0]


async def test_worker_discards_page_when_lease_is_lost_during_robots_wait():
    import asyncio

    class Repository:
        async def claim_task(self, worker_id, capabilities): return _task()
        async def heartbeat(self, task_id, lease_token): return False
        async def robots_cache(self, origin_key): return None
        async def store_robots(self, origin_key, body, status): pass

    class Scraper:
        async def scrape(self, url, **kwargs):
            raise AssertionError("lease loss during robots pacing must skip the page")

    async def robots(url):
        return {"status": 200, "html": "User-agent: *\nAllow: /\n", "headers": {}}

    await CrawlWorker("worker-1", {"http"}, Repository(), Scraper(),
                      heartbeat_seconds=0, robots_fetch=robots,
                      robots_sleep=lambda _: asyncio.sleep(0)).run_once()
