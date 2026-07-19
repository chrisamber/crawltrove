from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_avoids_known_copyleft_parser_dependencies():
    """Keep the MIT service independent from the former GPL/AGPL parsers."""
    requirements = (ROOT / "requirements.txt").read_text().lower()
    for package in ("html2text", "pymupdf", "pymupdf4llm"):
        assert package not in requirements

    imports = "\n".join(
        path.read_text(errors="ignore").lower()
        for folder in (ROOT / "app", ROOT / "tests")
        for path in folder.rglob("*.py")
        if path.resolve() != Path(__file__).resolve()
    )
    assert "import html2text" not in imports
    assert "import pymupdf" not in imports


def test_jobs_dashboard_escapes_server_and_user_controlled_text():
    source = (ROOT / "app" / "static" / "jobs.js").read_text()
    assert "${esc(job.schedule)}" in source
    assert source.count("${esc(e.message)}") >= 2


def test_signals_dashboard_only_links_http_urls():
    source = (ROOT / "app" / "static" / "signals.js").read_text()
    assert "function safeExternalLink" in source
    assert 'parsed.protocol !== "http:" && parsed.protocol !== "https:"' in source
    assert source.count("safeExternalLink(") >= 4
    assert "href=\"' + esc(match)" not in source
    assert "href=\"' + esc(l.url)" not in source
    assert "href=\"' + esc(extras.url)" not in source


def test_compose_isolates_renamed_storage_and_documents_upgrade():
    source = (ROOT / "docker-compose.yml").read_text()
    readme = (ROOT / "README.md").read_text()
    assert "- crawltrove_data:/workspace/data" in source
    assert "- crawltrove_pgdata:/var/lib/postgresql/data" in source
    assert "\n  crawltrove_data:\n" in source
    assert "\n  crawltrove_pgdata:\n" in source
    assert "appdata:" not in source
    assert "\n  pgdata:\n" not in source
    assert "## Upgrading from an earlier local checkout" in readme
    assert "cp -R /migration-source/. /workspace/data/" in readme
    assert "cp -a /migration-source" not in readme
    assert "permanently\ndeletes both the local database and all saved scrape artifacts" in readme


def test_compose_drops_privileges_without_host_ipc():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    service = compose["services"]["crawltrove"]

    assert "ipc" not in service
    assert service["cap_drop"] == ["ALL"]
    assert service["cap_add"] == ["SYS_CHROOT"]
    assert "no-new-privileges:true" in service["security_opt"]
    assert not any("/workspace/app" in volume for volume in service["volumes"])
