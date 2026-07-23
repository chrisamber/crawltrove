import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.conftest import requires_db


def test_metric_labels_are_bounded_and_unknown_values_are_normalized():
    from app.metrics import METRIC_LABELS, normalize_label

    forbidden = {
        "url", "origin", "job_id", "task_id", "worker_id", "session_id", "exception",
    }
    assert not forbidden.intersection(
        label for labels in METRIC_LABELS.values() for label in labels
    )
    assert normalize_label("route", "unbounded-route-name") == "unknown"
    assert normalize_label("provider", "firecrawl") == "firecrawl"
    assert normalize_label("signal", "keyword_db") == "keyword_db"
    assert "retrieval_degradations" in METRIC_LABELS


@pytest.mark.asyncio
async def test_readiness_reports_healthy_durable_dependencies():
    from app.routes.routes_operations import readiness_report

    report, ready = await readiness_report(
        checks={
            "database": True,
            "artifacts": True,
            "migrations": True,
            "browser": True,
            "leases": True,
        },
    )

    assert ready is True
    assert report == {
        "status": "ready",
        "database": "up",
        "artifacts": "up",
        "migrations": "compatible",
        "providers": "available",
    }


@pytest.mark.asyncio
async def test_readiness_fails_closed_when_lease_renewal_is_unavailable():
    from app.routes.routes_operations import readiness_report

    report, ready = await readiness_report(
        checks={
            "database": True,
            "artifacts": True,
            "migrations": True,
            "browser": True,
            "leases": False,
        },
    )

    assert ready is False
    assert report["status"] == "not_ready"


def test_leases_ready_fails_when_crawl_service_failed_to_start():
    from app.crawl.service import CrawlService

    service = CrawlService.__new__(CrawlService)
    service._started = False
    service._start_error = "RuntimeError: boom"
    service._maintenance_task = None
    service._maintenance_last_success = None
    service._maintenance_last_error = None
    assert service.leases_ready() is False


def test_migration_versions_are_public():
    from app.db import migrate

    versions = migrate.migration_versions()
    assert "0001_init" in versions
    assert "0012_queue_claim_performance" in versions
    assert versions == [version for version, _path in migrate._migration_files()]


def test_purge_requires_matching_uuid_confirmation_before_any_operation():
    from app.admin import validate_purge_confirmation

    job_id = "a9a0f640-2ad4-41eb-8bd2-953a84c1fa80"
    assert validate_purge_confirmation(job_id, job_id).hex == "a9a0f6402ad441eb8bd2953a84c1fa80"
    with pytest.raises(ValueError):
        validate_purge_confirmation(job_id, "different")
    with pytest.raises(ValueError):
        validate_purge_confirmation("not-a-uuid", "not-a-uuid")


def test_admin_exposes_only_the_documented_commands():
    from app.admin import COMMANDS

    assert COMMANDS == frozenset({
        "reconcile-job", "reap-leases", "list-failures", "validate-artifacts",
        "cleanup-temporary", "purge-job", "compatibility",
    })


@requires_db
async def test_failure_read_is_safe_against_a_fresh_durable_database(db):
    from app.admin import list_failures

    assert await list_failures() == []


def test_operations_reads_require_the_same_optional_auth_configuration(monkeypatch):
    from app.routes.routes_operations import health_router, metrics_router, router

    monkeypatch.setenv("APP_PASSWORD", "test-password")
    app = FastAPI()
    app.include_router(health_router)
    app.include_router(metrics_router)
    app.include_router(router)
    client = TestClient(app)

    assert client.get("/health/live").status_code == 200
    assert client.get("/metrics").status_code == 401
    assert client.get("/api/operations/providers").status_code == 401
    response = client.get("/api/operations/providers", auth=("admin", "test-password"))
    assert response.status_code == 200
    assert client.get("/metrics", auth=("admin", "test-password")).status_code == 200

    monkeypatch.setenv("METRICS_BIND_INTERNAL", "true")
    assert client.get("/metrics").status_code == 401
    monkeypatch.setenv("PUBLISHED_BIND_ADDRESS", "127.0.0.1")
    assert client.get("/metrics").status_code == 200


@requires_db
async def test_metrics_project_durable_managed_attempts_and_usage(db):
    from prometheus_client import generate_latest

    from app.crawl import repository
    from app.crawl.config import (
        AcquisitionConfig, CreditBudgets, CrawlConfig, FirecrawlBudget,
    )
    from app.routes.routes_operations import _refresh_metrics

    await repository.submit_job(CrawlConfig(
        url="https://example.com", minDelayMs=0,
        acquisition=AcquisitionConfig(
            creditBudgets=CreditBudgets(firecrawl=FirecrawlBudget(credits=2)),
        ),
    ))
    task = await repository.claim_task("metrics-worker", {"http"})
    assert task is not None
    attempt = await repository.reserve_acquisition_attempt(
        task.id, task.lease_token, "firecrawl_scrape", {"credits": 1},
    )
    assert attempt is not None
    assert await repository.finish_acquisition_attempt(
        attempt.id, task.lease_token, "succeeded", {"credits": 1},
    )

    await _refresh_metrics()
    payload = generate_latest().decode()
    assert (
        'crawltrove_acquisition_attempts_total{outcome="succeeded",'
        'provider="firecrawl",route="firecrawl_scrape"}' in payload
    )
    assert 'crawltrove_provider_usage_total{meter="credits",provider="firecrawl"}' in payload
