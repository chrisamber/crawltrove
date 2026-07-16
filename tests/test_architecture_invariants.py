"""Executable guards for the CLAUDE.md architectural invariants.

These are the merge gate for subagent-driven development: any change a
subagent returns must keep this file green. Each guard also proves it has
teeth by detecting a synthetic violation.
"""
import ast
import pathlib

import httpx
from httpx import ASGITransport

from app import lang, quality, scraper

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVICE_ROOT = REPO_ROOT / "app"

FORBIDDEN_PREFIXES = ("app.corpus", "scripts")


def _is_forbidden(module: str) -> bool:
    return any(module == p or module.startswith(p + ".") for p in FORBIDDEN_PREFIXES)


def forbidden_imports(source: str, *, module_path: str = "<mem>") -> list[str]:
    """Forbidden module names imported by `source` (one-way-dependency guard)."""
    tree = ast.parse(source, filename=module_path)
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            hits += [a.name for a in node.names if _is_forbidden(a.name)]
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            # Relative imports (level > 0) are intentionally out of scope: the
            # realistic violation is an absolute `from app.corpus import ...`.
            if node.level == 0 and _is_forbidden(mod):
                hits.append(mod)
    return hits


def _service_files() -> list[pathlib.Path]:
    return [
        p for p in SERVICE_ROOT.rglob("*.py")
        if "corpus" not in p.relative_to(SERVICE_ROOT).parts
    ]


def test_forbidden_imports_detector_has_teeth():
    # A synthetic violation MUST be flagged (proves the guard works at all).
    assert forbidden_imports("from app.corpus.router import route_record") == ["app.corpus.router"]
    assert forbidden_imports("import scripts.build_corpus") == ["scripts.build_corpus"]
    # Legitimate service imports MUST NOT be flagged.
    assert forbidden_imports("from app import quality, lang\nimport app.storage") == []


def test_service_never_imports_corpus_or_scripts():
    offenders = []
    for path in _service_files():
        for mod in forbidden_imports(path.read_text(), module_path=str(path)):
            offenders.append(f"{path.relative_to(REPO_ROOT)} -> {mod}")
    assert not offenders, "service imports forbidden modules:\n" + "\n".join(offenders)


def _page_html(body_words: int = 80) -> str:
    words = " ".join(["word"] * body_words)
    return f"<html><body><main>{words}</main></body></html>"


def test_quality_flags_junk_but_never_drops():
    report = quality.assess("x")  # far below MIN_WORDS
    assert isinstance(report, dict)            # a report, not a drop
    assert report["passed"] is False           # flagged...
    assert "word_count" in report["failures"]  # ...with the reason
    assert "signals" in report                 # and the raw signals kept


def test_lang_returns_none_not_raises_on_empty():
    assert lang.detect("") is None             # resilient default, no exception


def test_signal_failure_keeps_the_page(monkeypatch):
    # Force a signal to raise; the page must still succeed and the failure
    # must be reported in metadata.signal_errors (resilient-signals invariant).
    monkeypatch.setattr(
        quality, "assess",
        lambda md: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    result = scraper.WebScraper()._build_result(
        _page_html(), "http://example.test/x", True, "http",
    )
    assert result["success"] is True
    assert result["markdown"]                  # content preserved
    errors = result["metadata"].get("signal_errors", [])
    assert any(e["signal"] == "quality" for e in errors)


def test_license_detected_from_footer_that_cleaning_would_strip():
    # The marker lives ONLY in the footer. The cleaner keeps only the main
    # container (so the footer is dropped), proving license_detect ran on the
    # RAW html first.
    body = " ".join(["word"] * 80)
    html = (
        f"<html><body><main>{body}</main>"
        f"<footer>Text licensed under CC BY 4.0.</footer></body></html>"
    )
    result = scraper.WebScraper()._build_result(html, "http://example.test/x", True, "http")
    license_info = result["metadata"]["license"]
    assert license_info is not None
    assert license_info["id"] == "CC-BY-4.0"
    # And the footer text is NOT in the cleaned markdown (proves it was stripped).
    assert "licensed under" not in result["markdown"].lower()


def test_pool_disabled_without_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from app.db import pool
    assert pool.enabled() is False


async def test_db_routes_503_not_500_without_db(monkeypatch):
    # With no DB configured, DB-gated routes degrade to 503 — never a crash,
    # and never a changed scrape response.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from app.main import app
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.get("/api/export.csv")
    assert resp.status_code == 503


def test_deploy_stays_single_uvicorn_worker():
    # Single-process invariant: the in-memory dedup index, the crawler job store,
    # and the single-loop scheduler all assume one worker. Guard the deploy and
    # runtime configs against a multi-worker flag (`--workers N`) sneaking in.
    candidates = ["Dockerfile", "docker-compose.yml", "docker-compose.yaml",
                  ".github/workflows/ci.yml"]
    offenders = [
        rel for rel in candidates
        if (REPO_ROOT / rel).exists() and "--workers" in (REPO_ROOT / rel).read_text()
    ]
    assert not offenders, (
        "multi-worker flag found (breaks single-process invariant): "
        + ", ".join(offenders))
