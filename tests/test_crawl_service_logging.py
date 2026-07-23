"""Regression tests for durable crawl loop diagnostics (v0.4 audit)."""
import asyncio
from unittest.mock import AsyncMock

import pytest


async def test_worker_loop_logs_unexpected_exceptions(caplog):
    from app.crawl.service import CrawlService

    class BrokenWorker:
        worker_id = "worker-test"
        active_lease = None
        capabilities = set()

        async def run_once(self):
            raise RuntimeError("loop boom")

    service = CrawlService(worker=BrokenWorker())
    service._wake.set()

    async def stop_soon():
        await asyncio.sleep(0.05)
        await service.stop()

    with caplog.at_level("ERROR"):
        stopper = asyncio.create_task(stop_soon())
        await service.start()
        await stopper

    assert any(
        "crawl worker loop failed" in record.message and "worker-test" in record.message
        for record in caplog.records
    )
    assert any(record.exc_info for record in caplog.records)


async def test_maintenance_loop_stores_exception_message(caplog, monkeypatch):
    from app.crawl import service as service_mod

    async def boom():
        raise ValueError("db connection lost")

    monkeypatch.setattr(service_mod.repository, "reap_expired_leases", boom)
    monkeypatch.setattr(service_mod.sessions, "expire_due", AsyncMock())
    monkeypatch.setattr(service_mod.repository, "finalize_jobs", AsyncMock())

    class IdleWorker:
        worker_id = "worker-idle"
        active_lease = None
        capabilities = set()

        async def run_once(self):
            return False

    service = service_mod.CrawlService(worker=IdleWorker())

    async def stop_soon():
        await asyncio.sleep(0.05)
        await service.stop()

    with caplog.at_level("ERROR"):
        stopper = asyncio.create_task(stop_soon())
        await service.start()
        await stopper

    status = service.maintenance_status()
    assert status["last_error"] is not None
    assert "ValueError" in status["last_error"]
    assert "db connection lost" in status["last_error"]
    assert any("crawl maintenance loop failed" in record.message for record in caplog.records)


async def test_start_records_and_reraises_control_plane_failures(monkeypatch, caplog):
    from app.crawl import service as service_mod

    class IdleWorker:
        worker_id = "worker-idle"
        active_lease = None
        capabilities = {"http"}
        scraper = None

        async def run_once(self):
            return False

    def boom_create_task(coro, *_args, **_kwargs):
        # Close the coroutine so pytest does not warn about it never awaiting.
        if hasattr(coro, "close"):
            coro.close()
        raise RuntimeError("cannot schedule worker")

    service = service_mod.CrawlService(worker=IdleWorker())
    monkeypatch.setattr(service_mod.asyncio, "create_task", boom_create_task)

    with caplog.at_level("ERROR"):
        with pytest.raises(RuntimeError, match="cannot schedule worker"):
            await service.start()

    status = service.maintenance_status()
    assert status["started"] is False
    assert status["start_error"] is not None
    assert "RuntimeError" in status["start_error"]
    assert any("failed to start" in record.message for record in caplog.records)
