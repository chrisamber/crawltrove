"""Outbound run-completion webhooks.

No network: httpx.AsyncClient is monkeypatched with a recording fake. The DB hook
(record_run_finish -> webhooks.deliver) is covered against the live test DB.
"""
import hashlib
import hmac
import json

import pytest

from app import webhooks
from tests.conftest import requires_db


class _FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code


class _FakeClient:
    """Records each POST. Class-level knobs let a test force status / exception."""
    calls = []
    status_code = 200
    raise_exc = None

    def __init__(self, *a, **k):
        self.init_kwargs = k

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, content=None, headers=None):
        if _FakeClient.raise_exc:
            raise _FakeClient.raise_exc
        _FakeClient.calls.append({"url": url, "content": content, "headers": headers})
        return _FakeResponse(_FakeClient.status_code)


@pytest.fixture(autouse=True)
def _reset_fake():
    _FakeClient.calls = []
    _FakeClient.status_code = 200
    _FakeClient.raise_exc = None
    yield


def _run(**over):
    base = {
        "id": 7, "job_id": 3, "external_id": "abc", "trigger": "manual",
        "status": "completed", "engine_used": "http", "pages_count": 1,
        "error_message": None, "raw_output_path": "data/scrapes/abc.json",
        "started_at": "2026-06-29T00:00:00+00:00",
        "finished_at": "2026-06-29T00:00:01+00:00",
    }
    base.update(over)
    return base


# --- payload + signing (pure) ------------------------------------------------

def test_build_payload_maps_completed():
    p = webhooks.build_payload(_run())
    assert p["event"] == "run.completed"
    assert p["run"]["id"] == 7
    assert p["run"]["pages_count"] == 1
    assert p["run"]["external_id"] == "abc"


def test_build_payload_maps_failed():
    p = webhooks.build_payload(_run(status="failed", error_message="boom"))
    assert p["event"] == "run.failed"
    assert p["run"]["error_message"] == "boom"


def test_sign_matches_hmac_sha256():
    body = b'{"a":1}'
    expected = "sha256=" + hmac.new(b"s3cr3t", body, hashlib.sha256).hexdigest()
    assert webhooks._sign(body, "s3cr3t") == expected


# --- deliver (fake transport) ------------------------------------------------

async def test_deliver_noop_without_url(monkeypatch):
    monkeypatch.delenv("WEBHOOK_URL", raising=False)
    monkeypatch.setattr(webhooks.httpx, "AsyncClient", _FakeClient)
    assert await webhooks.deliver(_run()) is False
    assert _FakeClient.calls == []  # never touches the network


async def test_deliver_posts_signed_body(monkeypatch):
    monkeypatch.setenv("WEBHOOK_URL", "https://hook.example/x")
    monkeypatch.setenv("WEBHOOK_SECRET", "s3cr3t")
    monkeypatch.setattr(webhooks.httpx, "AsyncClient", _FakeClient)

    assert await webhooks.deliver(_run()) is True
    assert len(_FakeClient.calls) == 1
    call = _FakeClient.calls[0]
    assert call["url"] == "https://hook.example/x"
    assert json.loads(call["content"])["event"] == "run.completed"
    expected = webhooks._sign(call["content"], "s3cr3t")
    assert call["headers"]["X-CrawlTrove-Signature"] == expected


async def test_deliver_unsigned_without_secret(monkeypatch):
    monkeypatch.setenv("WEBHOOK_URL", "https://hook.example/x")
    monkeypatch.delenv("WEBHOOK_SECRET", raising=False)
    monkeypatch.setattr(webhooks.httpx, "AsyncClient", _FakeClient)
    await webhooks.deliver(_run())
    assert "X-CrawlTrove-Signature" not in _FakeClient.calls[0]["headers"]


async def test_deliver_swallows_network_error(monkeypatch):
    monkeypatch.setenv("WEBHOOK_URL", "https://hook.example/x")
    monkeypatch.setattr(webhooks.httpx, "AsyncClient", _FakeClient)
    _FakeClient.raise_exc = RuntimeError("connection refused")
    assert await webhooks.deliver(_run()) is False


async def test_deliver_false_on_error_status(monkeypatch):
    monkeypatch.setenv("WEBHOOK_URL", "https://hook.example/x")
    monkeypatch.setattr(webhooks.httpx, "AsyncClient", _FakeClient)
    _FakeClient.status_code = 500
    assert await webhooks.deliver(_run()) is False


# --- research events ----------------------------------------------------------

def _research_job(**over):
    base = {
        "job_id": "rj-1", "query": "what is x?", "status": "completed",
        "rounds_run": 2, "pages_scraped": 5, "llm_calls": 9,
        "sources": [{"index": 1}, {"index": 2}], "insufficient": False,
        "error": None, "artifact_stem": "stem-1",
        "start_time": "2026-07-10T00:00:00+00:00",
        "end_time": "2026-07-10T00:10:00+00:00",
    }
    base.update(over)
    return base


def test_build_research_payload_per_status():
    p = webhooks.build_research_payload(_research_job())
    assert p["event"] == "research.completed"
    assert p["research"]["sources_count"] == 2
    assert p["research"]["report_path"] == "data/research/stem-1.md"
    assert p["research"]["artifact_path"] == "data/research/stem-1.json"

    failed = webhooks.build_research_payload(
        _research_job(status="failed", error="boom", artifact_stem=None))
    assert failed["event"] == "research.failed"
    assert failed["research"]["error"] == "boom"
    assert failed["research"]["report_path"] is None

    cancelled = webhooks.build_research_payload(_research_job(status="cancelled"))
    assert cancelled["event"] == "research.cancelled"


async def test_deliver_research_posts_signed_body(monkeypatch):
    monkeypatch.setenv("WEBHOOK_URL", "https://hook.example/x")
    monkeypatch.setenv("WEBHOOK_SECRET", "s3cr3t")
    monkeypatch.setattr(webhooks.httpx, "AsyncClient", _FakeClient)

    assert await webhooks.deliver_research(_research_job()) is True
    call = _FakeClient.calls[0]
    assert json.loads(call["content"])["event"] == "research.completed"
    assert call["headers"]["X-CrawlTrove-Signature"] == webhooks._sign(
        call["content"], "s3cr3t")


async def test_deliver_research_noop_without_url(monkeypatch):
    monkeypatch.delenv("WEBHOOK_URL", raising=False)
    monkeypatch.setattr(webhooks.httpx, "AsyncClient", _FakeClient)
    assert await webhooks.deliver_research(_research_job()) is False
    assert _FakeClient.calls == []


# --- DB hook: record_run_finish dispatches on terminal status ----------------

@requires_db
async def test_record_run_finish_fires_webhook(db, monkeypatch):
    from app.db import repo
    monkeypatch.setenv("WEBHOOK_URL", "https://hook.example/x")
    monkeypatch.setattr(webhooks.httpx, "AsyncClient", _FakeClient)

    run_id = await repo.record_run_start(trigger="manual", status="processing")
    await repo.record_run_finish(run_id, status="completed", pages_count=1)

    assert len(_FakeClient.calls) == 1
    payload = json.loads(_FakeClient.calls[0]["content"])
    assert payload["event"] == "run.completed"
    assert payload["run"]["id"] == run_id


@requires_db
async def test_record_run_finish_no_webhook_when_url_unset(db, monkeypatch):
    from app.db import repo
    monkeypatch.delenv("WEBHOOK_URL", raising=False)
    monkeypatch.setattr(webhooks.httpx, "AsyncClient", _FakeClient)

    run_id = await repo.record_run_start(trigger="manual", status="processing")
    await repo.record_run_finish(run_id, status="completed")
    assert _FakeClient.calls == []
