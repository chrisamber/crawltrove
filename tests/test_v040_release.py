from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_v040_security_workflow_has_required_gates():
    source = (ROOT / ".github/workflows/security.yml").read_text(encoding="utf-8").lower()
    for gate in (
        "ruff", "mypy", "bandit", "pip-audit", "codeql", "gitleaks",
        "cyclonedx", "trivy",
    ):
        assert gate in source
    assert "contents: read" in source


def test_public_ci_has_no_provider_smoke_or_provider_secret_names():
    source = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "provider-smoke" not in source
    assert "secrets." not in source
    for secret in (
        "FIRECRAWL_API_KEY", "BRIGHTDATA_API_KEY", "BRIGHTDATA_UNLOCKER_ZONE",
        "BROWSERBASE_API_KEY", "BROWSERBASE_PROJECT_ID",
    ):
        assert secret not in source


def test_private_provider_smoke_is_manually_dispatched_in_a_protected_environment():
    source = (ROOT / ".github/workflows/provider-smoke.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in source
    assert "environment: provider-release" in source
    assert "python scripts/smoke_providers.py" in source
    for secret in ("FIRECRAWL_API_KEY", "BRIGHTDATA_API_KEY", "BROWSERBASE_API_KEY"):
        assert secret in source


def test_migration_compatibility_check_exercises_the_additive_upgrade_boundary():
    source = (ROOT / "scripts/check_migration_compat.py").read_text(encoding="utf-8")
    for migration in (
        "0001_init.sql", "0002_fts.sql", "0003_research_runs.sql",
        "0004_durable_crawl.sql", "0010_remote_managed_acquisition.sql",
        "0011_session_worker_protocol.sql", "0012_queue_claim_performance.sql",
    ):
        assert migration in source
    assert "scrape_runs" in source
    assert "scraped_pages" in source
    assert "run_migrations" in source
    assert "reset_pool" in source
    assert "sys.path.insert" in source


def test_migration_compatibility_database_guard_is_test_only():
    from scripts.check_migration_compat import is_compat_database

    assert is_compat_database("crawltrove_migration_compat_test")
    assert not is_compat_database("another_test")
    assert not is_compat_database("production")


def test_v040_release_metadata_is_complete():
    assert (ROOT / "app/VERSION").read_text(encoding="utf-8").strip() == "0.4.0"
    notes = (ROOT / "docs/release-v0.4.0.md").read_text(encoding="utf-8")
    for heading in (
        "## Migration", "## Remote workers", "## Provider budgets",
        "## Human intervention", "## Operations", "## Evaluation",
        "## Known limitations",
    ):
        assert heading in notes
    assert "100,000" in notes
    assert "Firecrawl" in notes
    assert "Crawl4AI" in notes
