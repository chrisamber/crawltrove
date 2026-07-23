import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]

# Public surfaces that must not leak private tracker / agent-process residue.
# Patterns stay narrow: product terms (egress agent, CSS linear-gradient,
# SHA-256, Swift Evolution SE-####, doc ids like ENG-001) must not match.
_PUBLIC_SCAN_ROOTS = (
    ROOT / "app",
    ROOT / "tests",
    ROOT / "docs",
    ROOT / "crawltrove_mcp",
    ROOT / "scripts",
    ROOT / "eval",
)
_PUBLIC_SCAN_FILES = (
    ROOT / "README.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / "SECURITY.md",
    ROOT / "AGENTS.md",
    ROOT / "THIRD_PARTY_NOTICES.md",
)
_PUBLIC_SUFFIXES = {".py", ".md", ".js", ".css", ".html", ".yml", ".yaml", ".txt"}
_SKIP_DIR_NAMES = {
    "__pycache__", ".pytest_cache", ".mypy_cache", "node_modules", "data",
}
# Private issue-tracker team prefixes only (not crypto/spec/product IDs).
# Extend this tuple when a new private backlog key starts leaking into git.
_PRIVATE_ISSUE_PREFIXES = ("SON",)


def _private_process_residue_re() -> re.Pattern[str]:
    issue_keys = "|".join(re.escape(p) for p in _PRIVATE_ISSUE_PREFIXES)
    return re.compile(
        r"(?ix)"
        r"("
        rf"\b(?:{issue_keys})-\d+\b"
        r"|linear\.app/"
        r"|\bE\d+\.S\d+\b"  # internal epic/story hierarchy, not product docs
        r"|\bEpic\s*[#:]?\s*\d+\b"
        r"|\bsuperpowers?\b"
        r"|\bneeds-human\b"
        r"|(?:generated|written)\ by\ (?:claude|codex|cursor|copilot|gpt|grok)\b"
        r")"
    )


_PRIVATE_PROCESS_RESIDUE = _private_process_residue_re()


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


def _public_text_files():
    files = []
    for root in _PUBLIC_SCAN_ROOTS:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in _PUBLIC_SUFFIXES:
                continue
            if any(part in _SKIP_DIR_NAMES for part in path.parts):
                continue
            # This file documents the banned patterns; skip self.
            if path.resolve() == Path(__file__).resolve():
                continue
            files.append(path)
    for path in _PUBLIC_SCAN_FILES:
        if path.is_file():
            files.append(path)
    return files


def test_no_private_tracker_or_agent_process_residue():
    """Fail when public text leaks ticket IDs, epics, or agent-process notes.

    Product terms (egress agent, AI extraction, CSS linear-gradient) are fine.
    Internal trackers and personal agent docs are not.
    """
    offenders = []
    for path in _public_text_files():
        text = path.read_text(errors="ignore")
        for match in _PRIVATE_PROCESS_RESIDUE.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            snippet = match.group(0).replace("\n", " ")[:80]
            offenders.append(f"{path.relative_to(ROOT)}:{line_no}: {snippet}")
    assert not offenders, (
        "private tracker / agent-process residue in public text:\n"
        + "\n".join(offenders)
    )
