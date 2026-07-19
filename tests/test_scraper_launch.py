"""Chromium launch policy: sandboxed by default with explicit local overrides."""
from app import scraper


def test_launch_kwargs_default(monkeypatch):
    monkeypatch.delenv("CHROMIUM_EXECUTABLE_PATH", raising=False)
    monkeypatch.delenv("CHROMIUM_DISABLE_SANDBOX", raising=False)
    kwargs = scraper._launch_kwargs()
    assert kwargs["headless"] is True
    assert kwargs["chromium_sandbox"] is True
    assert "--disable-dev-shm-usage" in kwargs["args"]
    assert "--no-sandbox" not in kwargs["args"]
    assert "--disable-setuid-sandbox" not in kwargs["args"]
    assert "executable_path" not in kwargs


def test_launch_kwargs_env_override(monkeypatch):
    monkeypatch.setenv("CHROMIUM_EXECUTABLE_PATH", "/opt/browsers/chrome")
    kwargs = scraper._launch_kwargs()
    assert kwargs["executable_path"] == "/opt/browsers/chrome"
    # The rest of the launch config is unchanged by the override.
    assert kwargs["headless"] is True
    assert "--disable-gpu" in kwargs["args"]


def test_launch_kwargs_explicit_unsafe_sandbox_override(monkeypatch):
    monkeypatch.setenv("CHROMIUM_DISABLE_SANDBOX", "true")
    kwargs = scraper._launch_kwargs()
    assert kwargs["chromium_sandbox"] is False
    assert "--no-sandbox" in kwargs["args"]


def test_browser_context_blocks_service_workers():
    assert scraper._context_kwargs()["service_workers"] == "block"
