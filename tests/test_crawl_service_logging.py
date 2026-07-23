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

    assert service._maintenance_last_error is not None
    assert "ValueError" in service._maintenance_last_error
    assert "db connection lost" in service._maintenance_last_error
    assert any("crawl maintenance loop failed" in record.message for record in caplog.records)
