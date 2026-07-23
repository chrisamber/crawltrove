import asyncio
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import WebSocketDisconnect

from app import scraper
from app.acquisition import owned_session
from app.acquisition.sessions import SessionHandle
from app.crawl.config import CrawlConfig
from app.crawl.types import ClaimedTask


async def test_owned_runtime_keeps_the_guarded_context_until_session_close(monkeypatch):
    async def public_url(_url):
        return ("203.0.113.10",)

    monkeypatch.setattr(scraper, "ensure_public_url", public_url)
    fulfilled = []
    websocket_closed = []
    transports = []

    class Transport:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.closed = 0
            transports.append(self)

        async def fetch_request(self, *args, **kwargs):
            return {"status": 200, "headers": {}, "content": b"ok"}

        async def close(self):
            self.closed += 1

    class Route:
        class request:
            url = "https://example.com"
            method = "GET"
            headers = {}
            post_data_buffer = None

            @staticmethod
            def is_navigation_request():
                return True

        async def abort(self, _reason):
            raise AssertionError("public route must be fulfilled")

        async def fulfill(self, **kwargs):
            fulfilled.append(kwargs)

    class WebsocketRoute:
        async def close(self, **kwargs):
            websocket_closed.append(kwargs)

    class Page:
        url = "https://example.com/final"

        async def goto(self, *_args, **_kwargs):
            await context.request_guard(Route())
            await context.websocket_guard(WebsocketRoute())
            return type("Response", (), {"status": 403})()

    class Context:
        closes = 0

        async def route(self, _pattern, guard):
            self.request_guard = guard

        async def route_web_socket(self, _pattern, guard):
            self.websocket_guard = guard

        async def new_page(self):
            return Page()

        async def close(self):
            self.closes += 1

    context = Context()

    class Browser:
        async def new_context(self, **kwargs):
            assert kwargs["proxy"] == {"server": "http://proxy"}
            return context

        async def close(self):
            pass

    class Playwright:
        class chromium:
            @staticmethod
            async def launch(**_kwargs):
                return Browser()

    class Manager:
        async def __aenter__(self):
            return Playwright()

        async def __aexit__(self, *_args):
            pass

    async def artifact_put(_screenshot):
        return "artifact://screenshot"

    runtime = scraper.BrowserRuntime(lambda: Manager(), max_contexts=1, transport_factory=Transport)
    session = await runtime.open_owned_session(
        "https://example.com", artifact_put=artifact_put, proxy={"server": "http://proxy"},
    )
    assert fulfilled == [{"status": 200, "headers": {}, "body": b"ok"}]
    assert websocket_closed == [{"code": 1008, "reason": "WebSockets disabled"}]
    assert context.closes == 0 and transports[0].closed == 0 and runtime._contexts.locked()

    await session.close()
    await session.close()
    assert context.closes == 1 and transports[0].closed == 1 and not runtime._contexts.locked()
    await runtime.close()


async def test_owned_session_resume_closes_on_validation_failure(monkeypatch):
    async def unsafe_url(_url):
        raise ValueError("unsafe")

    monkeypatch.setattr(owned_session, "ensure_public_url", unsafe_url)

    class Context:
        closes = 0

        async def close(self):
            self.closes += 1

    class Page:
        url = "http://127.0.0.1/"

    context = Context()
    session = owned_session.OwnedSessionContext(context, Page(), artifact_put=lambda _: None)
    with pytest.raises(ValueError, match="unsafe"):
        await session.resume(object())
    assert context.closes == 1 and session.closed is True


async def test_owned_session_screenshot_requires_a_bounded_artifact_reference():
    class Context:
        async def close(self):
            pass

    class Page:
        async def screenshot(self):
            return b"png"

    async def artifact_put(_screenshot):
        return "\nnot-an-artifact"

    session = owned_session.OwnedSessionContext(Context(), Page(), artifact_put=artifact_put)
    with pytest.raises(ValueError, match="artifact reference"):
        await session.execute({"action": "screenshot"})


async def test_owned_session_screenshots_share_one_cumulative_byte_budget():
    class Context:
        async def close(self):
            pass

    class Page:
        async def screenshot(self):
            return b"png"

    uploaded = []

    async def artifact_put(screenshot):
        uploaded.append(screenshot)
        return "sha256:" + "a" * 64

    session = owned_session.OwnedSessionContext(
        Context(), Page(), artifact_put=artifact_put, artifact_budget_bytes=5,
    )
    assert await session.execute({"action": "screenshot"}) == {
        "screenshot": {"artifactRef": "sha256:" + "a" * 64},
    }
    with pytest.raises(ValueError, match="byte budget"):
        await session.execute({"action": "screenshot"})
    assert uploaded == [b"png"]


class _Socket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.accepted = asyncio.Event()
        self.closed = []
        self.sent = []

    async def accept(self):
        self.accepted.set()

    async def close(self, **kwargs):
        self.closed.append(kwargs)

    async def receive_text(self):
        item = await self.incoming.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def send_json(self, value):
        self.sent.append(value)


async def test_tunnel_replacement_keeps_new_worker_and_rejects_raw_replies(monkeypatch):
    async def consume(*_args):
        return True

    async def touch(*_args):
        return True

    monkeypatch.setattr(owned_session.sessions, "consume_token", consume)
    monkeypatch.setattr(owned_session.sessions, "touch", touch)
    relay = owned_session.OwnedSessionTunnel()
    session_id = uuid4()
    old, new = _Socket(), _Socket()
    old_task = asyncio.create_task(relay.worker(old, session_id, "token"))
    await old.accepted.wait()
    new_task = asyncio.create_task(relay.worker(new, session_id, "token"))
    await new.accepted.wait()
    await old.incoming.put(WebSocketDisconnect(code=1000))
    await old_task
    assert relay._workers[session_id] is new

    await new.incoming.put(json.dumps({"id": "request", "result": {"rawUrl": "no"}}))
    await new_task
    assert new.closed == [{"code": 1008}]
    assert session_id not in relay._workers


def test_tunnel_rejects_worker_supplied_screenshot_urls():
    assert owned_session._valid_worker_result({
        "screenshot": {"artifactRef": "https://worker.invalid/live"},
    }) is None
    assert owned_session._valid_worker_result({
        "screenshot": {"artifactRef": "sha256:" + "a" * 64},
    }) is not None


async def test_tunnel_removes_pending_request_after_control_timeout(monkeypatch):
    async def touch(*_args):
        return True

    monkeypatch.setattr(owned_session.sessions, "touch", touch)
    monkeypatch.setattr(owned_session, "ACTION_DEADLINE_SECONDS", 0.01)
    relay = owned_session.OwnedSessionTunnel()
    session_id = uuid4()
    worker, control = _Socket(), _Socket()
    relay._workers[session_id] = worker
    relay.consume_bridge = lambda *_args: True
    await control.incoming.put(json.dumps({"action": "ping"}))
    await relay.control(control, session_id, "bridge")
    assert relay._pending == {}


async def test_runner_resumes_and_completes_a_parked_context():
    session_id = uuid4()
    task = ClaimedTask(
        id=uuid4(), job_id=uuid4(), url="https://example.com",
        normalized_url="https://example.com/", origin_key="https://example.com",
        depth=0, attempt=1, lease_token=uuid4(),
        deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        config=CrawlConfig(url="https://example.com").model_dump(),
        byte_allowance=1024 * 1024, artifact_allowance=1024 * 1024,
    )

    class Context:
        closed = False

        async def resume(self, _scraper):
            self.closed = True
            return {
                "url": "https://example.com/complete", "title": "Complete",
                "markdown": "done", "discovery_html": "<p>done</p>",
                "metadata": {"status_code": 200},
            }

        async def close(self):
            self.closed = True

    context = Context()

    class Browser:
        async def open_owned_session(self, *_args, **_kwargs):
            return context

    class Scraper:
        browser = Browser()

    class Repository:
        state = "waiting"
        completed = None

        async def start_live_session(self, *_args, **_kwargs):
            return SessionHandle(
                session_id, "owned", datetime.now(timezone.utc) + timedelta(minutes=5),
            )

        async def inspect_live_session(self, _session_id):
            return {"state": self.state}

        async def resume_live_session(self, _session_id, _worker_id):
            return task

        async def complete_task(self, _task_id, _lease_token, result):
            self.completed = result
            return True

        async def finish_live_session(self, _session_id):
            self.state = "closed"
            return True

    class Artifacts:
        async def put(self, *_args, **_kwargs):
            raise AssertionError("no screenshot expected")

    repository = Repository()
    runner = owned_session.OwnedSessionRunner(
        "browser-1", repository, Scraper(), Artifacts(),
    )
    assert await runner.start(task, {"before_browser": lambda: asyncio.sleep(0, result=True)})
    repository.state = "resuming"
    await asyncio.wait_for(next(iter(runner._tasks.values())), timeout=2)
    assert context.closed is True
    assert repository.completed.markdown == "done"
    assert repository.state == "closed"


async def test_remote_runner_completes_when_session_is_already_resuming():
    session_id = uuid4()
    task = ClaimedTask(
        id=uuid4(), job_id=uuid4(), url="https://example.com",
        normalized_url="https://example.com/", origin_key="https://example.com",
        depth=0, attempt=1, lease_token=uuid4(),
        deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        config=CrawlConfig(url="https://example.com").model_dump(),
        byte_allowance=1024, artifact_allowance=1024,
    )

    class Context:
        async def resume(self, _scraper):
            return {
                "url": task.url, "title": "done", "markdown": "done",
                "discovery_html": "<p>done</p>", "metadata": {"status_code": 200},
            }

        async def close(self):
            pass

    class Repository:
        completed = False

        async def inspect_live_session(self, _session_id):
            return {"state": "resuming"}

        async def resume_live_session(self, _session_id, _worker_id):
            return task

        async def complete_task(self, *_args):
            self.completed = True
            return True

        async def finish_live_session(self, _session_id):
            return True

    async def must_not_connect(*_args, **_kwargs):
        raise AssertionError("resuming session must complete before reconnect")

    repository = Repository()
    runner = owned_session.OwnedSessionRunner(
        "browser-1", repository, object(), object(),
        core_url="https://core.example", connect=must_not_connect,
    )
    await runner._remote_loop(session_id, task, Context())
    assert repository.completed is True


def test_remote_runner_requires_wss_unless_internal_plaintext_is_explicit(monkeypatch):
    runner = owned_session.OwnedSessionRunner(
        "browser-1", object(), object(), object(), core_url="http://core:8000",
    )
    with pytest.raises(ValueError, match="requires WSS"):
        runner._worker_tunnel_url(uuid4())
    monkeypatch.setenv("WORKER_ALLOW_INSECURE_CORE", "true")
    assert runner._worker_tunnel_url(uuid4()).startswith("ws://core:8000/api/acquisition/tunnel/")
