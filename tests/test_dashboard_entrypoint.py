from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_next_dashboard_is_preferred_when_built(monkeypatch):
    from app import main

    monkeypatch.setattr(
        main.os.path,
        "exists",
        lambda path: path == "app/static/dashboard/index.html",
    )
    monkeypatch.setattr(main, "FileResponse", lambda path: path)

    assert await main.serve_dashboard() == "app/static/dashboard/index.html"


def test_ci_verifies_frontend_and_served_dashboard():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "pnpm install --frozen-lockfile" in workflow
    assert "pnpm test" in workflow
    assert "pnpm check" in workflow
    assert workflow.count("working-directory: apps/app") >= 3
    assert "/static/dashboard/_next/" in workflow


def test_readme_uses_same_origin_development_proxy():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "FASTAPI_URL=http://127.0.0.1:8000 pnpm dev" in readme
    assert "NEXT_PUBLIC_API_URL=http://localhost:8000 pnpm dev" not in readme
