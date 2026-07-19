import pytest


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
